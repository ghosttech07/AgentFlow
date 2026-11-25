"""LLM error classes. The engine's retry policy (Part 5) keys off `error_type`, so be precise.

| Class                    | error_type      | Cause                                              | Retry? | HTTP |
|--------------------------|-----------------|----------------------------------------------------|--------|------|
| LLMTransientError        | transient       | timeout, 408, per-minute 429, 500/502/504, network | yes    | 503  |
| LLMOverloadedError       | overloaded      | 503 UNAVAILABLE / "high demand" / overloaded       | yes*   | 503  |
| LLMQuotaExhaustedError   | quota_exhausted | 429 per-day quota, quota limit 0, billing/plan     | no     | 429  |
| LLMPermanentError        | permanent       | 400/401/403, safety block, missing key             | no     | 502  |
| LLMModelNotFoundError    | permanent       | 404 model not found / retired                      | no*    | 502  |
| LLMOutputError           | output          | not JSON / schema mismatch after one repair        | yes    | 502  |
(*) overloaded and model-not-found also trigger the model fallback chain inside one call.
`retry_after_seconds` is parsed from RetryInfo.retryDelay or a Retry-After header when present.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from google.genai import errors as genai_errors

from app.core.errors import AppError

try:  # aiohttp is an optional transport of google-genai
    import aiohttp

    _AIOHTTP_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ClientError,)
except ImportError:  # pragma: no cover
    _AIOHTTP_ERRORS = ()


class LLMError(AppError):
    status_code = 502
    code = "llm_error"
    retryable = False
    error_type = "unknown"

    def __init__(
        self, message: str, *, details: Any | None = None, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after_seconds = retry_after_seconds


class LLMTransientError(LLMError):
    status_code = 503
    code = "llm_unavailable"
    retryable = True
    error_type = "transient"


class LLMOverloadedError(LLMTransientError):
    code = "llm_overloaded"
    error_type = "overloaded"


class LLMTimeoutError(LLMTransientError):
    """The provider did not answer within the call timeout (falls back to the next route entry)."""

    code = "llm_timeout"
    error_type = "timeout"


class LLMQuotaExhaustedError(LLMError):
    status_code = 429
    code = "llm_quota_exhausted"
    error_type = "quota_exhausted"


class LLMPermanentError(LLMError):
    code = "llm_request_failed"
    error_type = "permanent"


class LLMAuthError(LLMPermanentError):
    """Bad / missing / unauthorised key: a misconfiguration, never routed around silently."""

    code = "llm_auth"


class LLMModelNotFoundError(LLMPermanentError):
    code = "llm_model_not_found"


class BudgetPause(Exception):  # noqa: N818 - a control signal, not an error
    """Raised by the router when a call would breach the workflow budget (the workflow is paused)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LLMOutputError(LLMError):
    code = "llm_invalid_output"
    retryable = True  # a fresh attempt often produces valid output
    error_type = "output"


TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    ConnectionError,
    *_AIOHTTP_ERRORS,
)
_OVERLOAD_RE = re.compile(r"high demand|overloaded|try again later", re.I)
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def classify_exception(exc: BaseException) -> LLMError:
    """Map any exception raised while calling the SDK to an LLMError subclass."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError is TimeoutError on 3.11+
        return LLMTimeoutError("Gemini request timed out")
    if isinstance(exc, genai_errors.APIError):
        return _classify_api_error(exc)
    if isinstance(exc, NETWORK_ERRORS):
        return LLMTransientError(f"Network error calling Gemini: {type(exc).__name__}")
    # Unknown error = likely a bug on our side; retrying would not help.
    return LLMPermanentError(f"Unexpected error calling Gemini: {type(exc).__name__}")


def _classify_api_error(exc: genai_errors.APIError) -> LLMError:
    code = exc.code or 0
    status = exc.status or ""
    raw = exc.message or ""
    message = f"Gemini API error {code} {status}: {raw}".strip()
    error_body = _error_body(exc.details)
    retry_after = _retry_after(error_body, exc)
    kwargs: dict[str, Any] = {"details": {"http_status": code}, "retry_after_seconds": retry_after}

    if code == 429:
        if _is_hard_quota(error_body, raw):
            return LLMQuotaExhaustedError(message, **kwargs)
        return LLMTransientError(message, **kwargs)  # per-minute rate limit
    if code == 503 or (code >= 500 and _OVERLOAD_RE.search(raw)):
        return LLMOverloadedError(message, **kwargs)
    if code == 404:
        return LLMModelNotFoundError(message, **kwargs)
    if code in (401, 403) or (code == 400 and "api key" in raw.lower()):
        return LLMAuthError(message, **kwargs)
    if code in TRANSIENT_HTTP_CODES or code >= 500:
        return LLMTransientError(message, **kwargs)
    return LLMPermanentError(message, **kwargs)


def _error_body(details: Any) -> dict[str, Any]:
    if isinstance(details, list) and details:
        details = details[0]
    if isinstance(details, dict):
        return details.get("error", details) if isinstance(details.get("error"), dict) else details
    return {}


def _detail_items(error_body: dict[str, Any], type_suffix: str) -> list[dict[str, Any]]:
    return [d for d in error_body.get("details") or []
            if isinstance(d, dict) and str(d.get("@type", "")).endswith(type_suffix)]


def _is_hard_quota(error_body: dict[str, Any], message: str) -> bool:
    """True if retrying soon cannot help: daily / zero-limit quota or plan/billing limits.

    Per-minute rate limits (QuotaFailure ids with 'PerMinute') stay transient.
    """
    violations = [v for qf in _detail_items(error_body, "QuotaFailure") for v in qf.get("violations") or []]
    for v in violations:
        ident = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')} {v.get('description', '')}"
        if re.search(r"PerDay|per day|daily", ident, re.I) or str(v.get("quotaValue")) == "0":
            return True
    if violations:
        return False  # only per-minute style violations
    if re.search(r"per day|limit: 0\b", message, re.I):
        return True
    # A quota 429 without any QuotaFailure/RetryInfo details is a plan/billing limit
    # (e.g. the Google Search grounding quota on this key): not fixed by waiting.
    return "exceeded your current quota" in message and not _detail_items(error_body, "RetryInfo")


def _retry_after(error_body: dict[str, Any], exc: genai_errors.APIError) -> float | None:
    for info in _detail_items(error_body, "RetryInfo"):
        match = _DURATION_RE.match(str(info.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None
