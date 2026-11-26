"""GeminiClient error mapping and repair logic, using a stub SDK (no network)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from app.config import get_settings
from app.llm.errors import (
    LLMOutputError,
    LLMPermanentError,
    LLMTransientError,
    classify_exception,
)
from app.llm.gemini_client import GeminiClient


class Out(BaseModel):
    answer: str


def api_error(code: int) -> genai_errors.APIError:
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "message": "boom", "status": "X"}})


def response(text: str | None, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=5, candidates_token_count=3, thoughts_token_count=None
        ),
        prompt_feedback=extra.get("prompt_feedback"),
        candidates=extra.get("candidates", []),
    )


class StubSDK:
    """Mimics `client.aio.models.generate_content`; replays queued results."""

    def __init__(self, *results: Any, delay: float = 0) -> None:
        self.results = list(results)
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def client_with(sdk: StubSDK, **settings: Any) -> GeminiClient:
    return GeminiClient(get_settings().model_copy(update=settings), sdk_client=sdk)


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
def test_transient_http_codes(code: int) -> None:
    err = classify_exception(api_error(code))
    assert isinstance(err, LLMTransientError) and err.retryable


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_permanent_http_codes(code: int) -> None:
    err = classify_exception(api_error(code))
    assert isinstance(err, LLMPermanentError) and not err.retryable


@pytest.mark.parametrize(
    "exc", [TimeoutError(), TimeoutError(), httpx.ConnectError("x"), ConnectionResetError()]
)
def test_timeouts_and_network_errors_are_transient(exc: BaseException) -> None:
    assert isinstance(classify_exception(exc), LLMTransientError)


def test_unknown_exception_is_permanent() -> None:
    assert isinstance(classify_exception(KeyError("x")), LLMPermanentError)


async def test_sdk_error_is_mapped_by_client() -> None:
    with pytest.raises(LLMTransientError):
        await client_with(StubSDK(api_error(429))).generate_structured("s", "u", Out)


async def test_timeout_raises_transient_error() -> None:
    sdk = StubSDK(response('{"answer": "late"}'), delay=1)
    with pytest.raises(LLMTransientError, match="timed out"):
        await client_with(sdk, gemini_timeout_seconds=0.05).generate_structured("s", "u", Out)


async def test_valid_json_is_parsed_with_usage() -> None:
    result = await client_with(StubSDK(response('{"answer": "42"}'))).generate_structured("s", "u", Out)
    assert result.data == Out(answer="42")
    assert (result.input_tokens, result.output_tokens) == (5, 3)


async def test_invalid_json_gets_exactly_one_repair_then_fails() -> None:
    sdk = StubSDK(response("not json"), response('{"wrong": 1}'), response('{"answer": "never"}'))
    with pytest.raises(LLMOutputError):
        await client_with(sdk).generate_structured("s", "u", Out)
    assert len(sdk.calls) == 2  # original + one repair, never a third
    assert "PREVIOUS RESPONSE:\nnot json" in sdk.calls[1]["contents"]


async def test_repair_succeeds() -> None:
    sdk = StubSDK(response("```json\n{bad"), response('{"answer": "fixed"}'))
    result = await client_with(sdk).generate_structured("s", "u", Out)
    assert result.data.answer == "fixed"
    assert result.input_tokens == 10  # usage summed across both calls


async def test_safety_block_is_permanent() -> None:
    blocked = response(None, prompt_feedback=SimpleNamespace(block_reason="SAFETY"))
    with pytest.raises(LLMPermanentError, match="safety"):
        await client_with(StubSDK(blocked)).generate_structured("s", "u", Out)


async def test_missing_api_key_is_permanent() -> None:
    client = GeminiClient(get_settings().model_copy(update={"gemini_api_key": None}))
    with pytest.raises(LLMPermanentError, match="GEMINI_API_KEY"):
        await client.generate_structured("s", "u", Out)


async def test_two_step_grounding_for_models_without_grounded_json() -> None:
    web = SimpleNamespace(uri="https://example.com/ev", title="example.com", domain=None)
    grounded = response(
        "EV sales grew.",
        candidates=[SimpleNamespace(
            finish_reason=None,
            grounding_metadata=SimpleNamespace(grounding_chunks=[SimpleNamespace(web=web)]),
        )],
    )
    sdk = StubSDK(grounded, response('{"answer": "ok"}'))
    result = await client_with(sdk, gemini_model="gemini-2.5-flash").generate_structured(
        "s", "u", Out, use_search=True
    )
    first, second = sdk.calls
    assert first["config"].tools and first["config"].response_mime_type is None
    assert not second["config"].tools and second["config"].response_mime_type == "application/json"
    assert "EV sales grew." in second["contents"]
    assert [s.url for s in result.sources] == ["https://example.com/ev"]


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gemini-2.5-flash", False), ("gemini-3.8-flash", True), ("gemini-4-neon", True),
     ("gemini-flash-latest", False)],
)
def test_grounded_json_support_by_model(model: str, expected: bool) -> None:
    assert client_with(StubSDK(), gemini_model=model).grounded_json_supported() is expected


async def test_single_step_grounding_for_gemini_3() -> None:
    sdk = StubSDK(response('{"answer": "ok"}'))
    await client_with(sdk, gemini_model="gemini-3-flash").generate_structured(
        "s", "u", Out, use_search=True
    )
    (call,) = sdk.calls
    assert call["config"].tools and call["config"].response_mime_type == "application/json"


# --------------------------------------------------------------------- Part 8 taxonomy additions

def test_timeout_maps_to_timeout_error_type() -> None:
    from app.llm.errors import LLMTimeoutError, classify_exception

    err = classify_exception(TimeoutError())
    assert isinstance(err, LLMTimeoutError) and err.error_type == "timeout" and err.retryable


def test_auth_errors_map_to_auth_class() -> None:
    from app.llm.errors import LLMAuthError, LLMPermanentError, classify_exception
    from tests.test_errors_classification import BAD_KEY, api_error

    unauthorized = {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "Request had invalid credentials."}}
    forbidden = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Permission denied."}}
    for payload in (unauthorized, forbidden, BAD_KEY):
        err = classify_exception(api_error(payload))
        assert isinstance(err, LLMAuthError) and isinstance(err, LLMPermanentError), payload
        assert err.error_type == "permanent"


def test_llm_response_has_provider_cost_and_route_fields() -> None:
    from app.llm.base import LLMResponse

    r = LLMResponse(data=None, raw_text="", model="m")  # type: ignore[arg-type]
    assert (r.provider, r.cost_usd, r.fallback_from, r.route_position) == ("gemini", None, None, 0)


def test_part8_settings_defaults() -> None:
    from app.config import Settings

    f = Settings.model_fields
    assert f["ollama_base_url"].default == "http://localhost:11434/v1"
    assert f["quota_timezone"].default == "UTC"
    assert f["default_max_llm_calls"].default == 50
    assert f["default_max_cost_usd"].default is None and f["default_max_tokens"].default is None
    assert f["default_max_duration_seconds"].default is None
    assert str(f["models_config_path"].default).replace("\\", "/") == "config/models.yaml"
    assert str(f["pricing_config_path"].default).replace("\\", "/") == "config/pricing.yaml"


def test_empty_budget_env_values_mean_unlimited() -> None:
    from app.config import Settings

    s = Settings(default_max_cost_usd="", default_max_tokens="", default_max_duration_seconds="",
                 anthropic_api_key="", openai_api_key="")
    assert (s.default_max_cost_usd, s.default_max_tokens, s.default_max_duration_seconds) == (None, None, None)
    assert s.anthropic_api_key is None and s.openai_api_key is None
