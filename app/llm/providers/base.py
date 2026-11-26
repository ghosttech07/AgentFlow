"""Provider adapter contract + helpers shared by every provider.

An adapter makes ONE call to ONE model and maps its errors into the app.llm.errors taxonomy. It never
falls back, retries or counts quota: the router (app.llm.router) decides all of that in code.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMResponse
from app.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMModelNotFoundError,
    LLMOutputError,
    LLMOverloadedError,
    LLMPermanentError,
    LLMQuotaExhaustedError,
    LLMTimeoutError,
    LLMTransientError,
)

# thinking_level -> the "effort" vocabulary OpenAI (reasoning_effort) and Anthropic (output_config.effort) use
EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}
_QUOTA_RE = re.compile(r"insufficient_quota|exceeded your current quota|billing|spend limit|usage limit|"
                       r"credit balance|payment required|per day|daily", re.I)
_RETRY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


class ProviderAdapter(Protocol):
    name: str

    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        *,
        use_search: bool,
        thinking_level: str | None,
    ) -> LLMResponse: ...


def status_error(provider: str, status: int, message: str, retry_after: float | None = None) -> LLMError:
    """HTTP status (+ message wording for 429) -> taxonomy class. Shared by the non-Gemini adapters."""
    text = f"{provider} API error {status}: {message}"
    kw = {"details": {"http_status": status, "provider": provider}, "retry_after_seconds": retry_after}
    # Billing / credit / quota exhaustion: route around it (Anthropic sends "credit balance is too low" as 400).
    if status in (400, 402, 429) and _QUOTA_RE.search(message):
        return LLMQuotaExhaustedError(text, **kw)
    if status == 429:
        return LLMTransientError(text, **kw)
    if status in (503, 529):
        return LLMOverloadedError(text, **kw)
    if status in (401, 403):
        return LLMAuthError(text, **kw)
    if status == 404:
        return LLMModelNotFoundError(text, **kw)
    if status == 408:
        return LLMTimeoutError(text, **kw)
    if status >= 500:
        return LLMTransientError(text, **kw)
    return LLMPermanentError(text, **kw)


def retry_after_header(headers: object) -> float | None:
    value = getattr(headers, "get", lambda _k: None)("retry-after")
    match = _RETRY_RE.match(value) if isinstance(value, str) else None
    return float(match.group(1)) if match else None


def parse_json(raw: str, output_model: type[BaseModel], provider: str) -> BaseModel:
    """Validate raw model text against the schema (tolerating markdown fences)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    if not text:
        raise LLMOutputError(f"{provider} returned an empty response")
    try:
        return output_model.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:10]
        )
        raise LLMOutputError(f"{provider} output failed validation: {problems}") from exc


def repair_prompt(prompt: str, bad_output: str, error: str) -> str:
    return (
        f"{prompt}\n\n---\nYour previous response was invalid.\n"
        f"PREVIOUS RESPONSE:\n{(bad_output or '')[:8000]}\n\nERROR:\n{error}\n\n"
        "Return ONLY the corrected JSON object that matches the required schema."
    )
