"""Classification of real google-genai error payloads (captured from this API key, 2026-11-24)."""

from __future__ import annotations

import copy

import httpx
import pytest
from google.genai import errors as genai_errors

from app.llm.errors import (
    LLMAuthError,
    LLMModelNotFoundError,
    LLMOverloadedError,
    LLMPermanentError,
    LLMQuotaExhaustedError,
    LLMTransientError,
    classify_exception,
)

QUOTA_MSG = ("You exceeded your current quota, please check your plan and billing details. For more "
             "information on this error, head to: https://aistudio.google.com/docs/rate-limits. To "
             "monitor your current usage, head to: https://ai.dev/rate-limit. ")
HELP = {"@type": "type.googleapis.com/google.rpc.Help",
        "links": [{"description": "Learn more about Gemini API quotas",
                   "url": "https://aistudio.google.com/docs/rate-limits"}]}

# Real: free-tier DAILY limit on gemini-3.5-flash.
DAILY_QUOTA = {"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED",
    "message": QUOTA_MSG + "\n* Quota exceeded for metric: generativelanguage.googleapis.com/"
               "generate_content_free_tier_requests, limit: 20, model: gemini-3.6-flash, description: "
               "Generate content requests per day limit exceeded on Free Tier.\nPlease retry in 16h30m59s.",
    "details": [HELP,
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                    "description": "Generate content requests per day limit exceeded on Free Tier.",
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaDimensions": {"model": "gemini-3.6-flash", "location": "global"},
                    "quotaValue": "20"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "59459s"}]}}

# Real: Google Search grounding on this key (no QuotaFailure, no RetryInfo).
GROUNDING_QUOTA = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": QUOTA_MSG,
                             "details": [HELP]}}

# Real: 503 overload.
OVERLOADED = {"error": {"code": 503, "status": "UNAVAILABLE",
                        "message": "This model is currently experiencing high demand. Spikes in demand "
                                   "are usually temporary. Please try again later."}}

# Per-minute rate limit (same shape as the daily one, with a PerMinute quota id).
PER_MINUTE = copy.deepcopy(DAILY_QUOTA)
PER_MINUTE["error"]["message"] = QUOTA_MSG + "\nPlease retry in 23.4s."
PER_MINUTE["error"]["details"][1]["violations"][0].update(
    quotaId="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", quotaValue="10",
    description="Generate content requests per minute limit exceeded.")
PER_MINUTE["error"]["details"][2]["retryDelay"] = "23.4s"

ZERO_LIMIT = copy.deepcopy(PER_MINUTE)
ZERO_LIMIT["error"]["details"][1]["violations"][0]["quotaValue"] = "0"

MODEL_NOT_FOUND = {"error": {"code": 404, "status": "NOT_FOUND",
                             "message": "models/gemini-2.5-flash is no longer available to new users."}}
BAD_KEY = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid."}}


def api_error(payload: dict, response: httpx.Response | None = None) -> genai_errors.APIError:
    code = payload["error"]["code"]
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, payload, response)


@pytest.mark.parametrize(
    ("payload", "cls", "error_type", "retryable", "retry_after"),
    [
        (OVERLOADED, LLMOverloadedError, "overloaded", True, None),
        (PER_MINUTE, LLMTransientError, "transient", True, 23.4),
        (DAILY_QUOTA, LLMQuotaExhaustedError, "quota_exhausted", False, 59459.0),
        (GROUNDING_QUOTA, LLMQuotaExhaustedError, "quota_exhausted", False, None),
        (ZERO_LIMIT, LLMQuotaExhaustedError, "quota_exhausted", False, 23.4),
        (MODEL_NOT_FOUND, LLMModelNotFoundError, "permanent", False, None),
        (BAD_KEY, LLMAuthError, "permanent", False, None),  # Part 8: auth is its own subclass
    ],
    ids=["overloaded-503", "rate-limit-per-minute", "daily-quota", "grounding-quota",
         "zero-limit", "model-not-found", "bad-request"],
)
def test_real_payloads(payload: dict, cls: type, error_type: str, retryable: bool,
                       retry_after: float | None) -> None:
    err = classify_exception(api_error(payload))
    assert type(err) is cls
    assert err.error_type == error_type and err.retryable is retryable
    assert err.retry_after_seconds == retry_after


def test_overloaded_is_a_transient_error() -> None:
    err = classify_exception(api_error(OVERLOADED))
    assert isinstance(err, LLMTransientError) and err.status_code == 503


def test_quota_exhausted_http_mapping() -> None:
    err = classify_exception(api_error(DAILY_QUOTA))
    assert err.status_code == 429 and err.code == "llm_quota_exhausted"


def test_retry_after_header_used_when_no_retry_info() -> None:
    payload = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Too many requests",
                         "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                      "violations": [{"quotaId": "RequestsPerMinute"}]}]}}
    response = httpx.Response(429, headers={"Retry-After": "7"})
    err = classify_exception(api_error(payload, response))
    assert isinstance(err, LLMTransientError) and err.retry_after_seconds == 7.0


def test_other_5xx_is_transient_not_overloaded() -> None:
    payload = {"error": {"code": 500, "status": "INTERNAL", "message": "Internal error"}}
    err = classify_exception(api_error(payload))
    assert type(err) is LLMTransientError and err.error_type == "transient"
