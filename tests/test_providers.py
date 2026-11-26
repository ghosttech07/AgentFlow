"""Provider adapters (Part 8): mocked SDK clients with realistic payloads; no network."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from pydantic import BaseModel, SecretStr

from app.config import get_settings
from app.llm.errors import (
    LLMAuthError,
    LLMModelNotFoundError,
    LLMOutputError,
    LLMOverloadedError,
    LLMPermanentError,
    LLMQuotaExhaustedError,
    LLMTimeoutError,
    LLMTransientError,
)
from app.llm.providers import registry
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.base import status_error
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.openai_provider import OpenAICompatProvider
from tests.test_errors_classification import OVERLOADED, api_error
from tests.test_model_fallback import ModelStub, ok


class Out(BaseModel):
    answer: str


def http_response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers or {}, request=httpx2.Request("POST", "https://api.example/v1"))


def openai_error(cls: type, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Exception:
    return cls(body["error"]["message"], response=http_response(status, headers), body=body)


def anthropic_error(cls: type, status: int, etype: str, message: str, headers: dict[str, str] | None = None) -> Exception:
    body = {"type": "error", "error": {"type": etype, "message": message}}
    return cls(message, response=http_response(status, headers), body=body)


# ------------------------------------------------------------------------------ OpenAI / Ollama

class FakeOpenAI:
    """Minimal async openai client: chat.completions.parse returns scripted results in order."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    async def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def completion(content: str, parsed: BaseModel | None, refusal: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, parsed=parsed, refusal=refusal),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )


async def test_openai_structured_call_returns_llm_response() -> None:
    client = FakeOpenAI(completion('{"answer": "42"}', Out(answer="42")))
    p = OpenAICompatProvider("openai", client)
    r = await p.generate("gpt-x", "sys", "user", Out, use_search=True, thinking_level="high")
    assert (r.provider, r.model, r.data.answer, r.input_tokens, r.output_tokens) == ("openai", "gpt-x", "42", 120, 30)
    call = client.calls[0]
    assert call["response_format"] is Out and call["reasoning_effort"] == "high"
    assert call["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]


async def test_openai_repairs_invalid_output_once() -> None:
    client = FakeOpenAI(completion("not json", None), completion('{"answer": "fixed"}', Out(answer="fixed")))
    r = await OpenAICompatProvider("openai", client).generate("m", "s", "u", Out, use_search=False, thinking_level=None)
    assert r.data.answer == "fixed" and len(client.calls) == 2
    assert "previous response was invalid" in client.calls[1]["messages"][-1]["content"].lower()
    bad = FakeOpenAI(completion("nope", None), completion("still nope", None))
    with pytest.raises(LLMOutputError):
        await OpenAICompatProvider("openai", bad).generate("m", "s", "u", Out, use_search=False, thinking_level=None)


async def test_openai_drops_reasoning_effort_for_models_that_reject_it() -> None:
    rejected = openai_error(openai.BadRequestError, 400, {"error": {
        "message": "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
        "type": "invalid_request_error", "param": "reasoning_effort", "code": "unsupported_parameter"}})
    client = FakeOpenAI(rejected, completion('{"answer": "a"}', Out(answer="a")),
                        completion('{"answer": "b"}', Out(answer="b")))
    p = OpenAICompatProvider("openai", client)
    assert (await p.generate("m", "s", "u", Out, use_search=False, thinking_level="low")).data.answer == "a"
    await p.generate("m", "s", "u", Out, use_search=False, thinking_level="low")
    assert "reasoning_effort" not in client.calls[1] and "reasoning_effort" not in client.calls[2]


async def test_ollama_model_without_thinking_drops_reasoning_effort() -> None:
    # Real Ollama 0.40 reply for llama3.2:3b when reasoning_effort is sent (captured live 2026-11-26).
    rejected = openai_error(openai.BadRequestError, 400, {"error": {
        "message": '"llama3.2:3b" does not support thinking', "type": "invalid_request_error", "param": None,
        "code": None}})
    client = FakeOpenAI(rejected, completion('{"answer": "a"}', Out(answer="a")))
    r = await OpenAICompatProvider("ollama", client).generate("llama3.2:3b", "s", "u", Out, use_search=False,
                                                              thinking_level="low")
    assert r.data.answer == "a" and "reasoning_effort" not in client.calls[1]


OPENAI_ERRORS = [
    (openai_error(openai.RateLimitError, 429, {"error": {"message": "Rate limit reached for requests",
                                                         "type": "requests", "code": "rate_limit_exceeded"}},
                  {"retry-after": "7"}), LLMTransientError, 7.0),
    (openai_error(openai.RateLimitError, 429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details.",
                                                         "type": "insufficient_quota", "code": "insufficient_quota"}}),
     LLMQuotaExhaustedError, None),
    (openai_error(openai.InternalServerError, 503, {"error": {"message": "The server is overloaded or not ready yet.",
                                                              "type": "server_error"}}), LLMOverloadedError, None),
    (openai_error(openai.AuthenticationError, 401, {"error": {"message": "Incorrect API key provided: sk-...xyz.",
                                                              "type": "invalid_request_error", "code": "invalid_api_key"}}),
     LLMAuthError, None),
    (openai_error(openai.NotFoundError, 404, {"error": {"message": "The model `gpt-nope` does not exist",
                                                        "type": "invalid_request_error", "code": "model_not_found"}}),
     LLMModelNotFoundError, None),
    (openai.APITimeoutError(request=httpx2.Request("POST", "https://api.example/v1")), LLMTimeoutError, None),
    # unreachable (e.g. Ollama stopped) = unavailable -> overloaded, so the router falls back (review I-4)
    (openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:11434/v1")), LLMOverloadedError, None),
]


@pytest.mark.parametrize(("error", "expected", "retry_after"), OPENAI_ERRORS,
                         ids=["rate-limit", "insufficient-quota", "overloaded", "auth", "model-not-found",
                              "timeout", "connection"])
async def test_openai_error_mapping(error: Exception, expected: type, retry_after: float | None) -> None:
    with pytest.raises(expected) as exc:
        await OpenAICompatProvider("openai", FakeOpenAI(error)).generate("m", "s", "u", Out, use_search=False,
                                                                          thinking_level=None)
    assert type(exc.value) is expected and exc.value.retry_after_seconds == retry_after


async def test_openai_refusal_is_permanent() -> None:
    client = FakeOpenAI(completion("", None, refusal="I can't help with that."))
    with pytest.raises(LLMPermanentError):
        await OpenAICompatProvider("openai", client).generate("m", "s", "u", Out, use_search=False, thinking_level=None)


# ------------------------------------------------------------------------------ Anthropic

class FakeAnthropic:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(parse=self._parse)

    async def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def claude_message(parsed: BaseModel | None, text: str = '{"answer": "x"}', stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason,
                           content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=200, output_tokens=40))


async def test_anthropic_structured_call_with_effort() -> None:
    client = FakeAnthropic(claude_message(Out(answer="claude")))
    r = await AnthropicProvider(client).generate("claude-x", "sys", "user", Out, use_search=False, thinking_level="minimal")
    assert (r.provider, r.data.answer, r.input_tokens, r.output_tokens) == ("anthropic", "claude", 200, 40)
    call = client.calls[0]
    assert call["output_format"] is Out and call["system"] == "sys"
    assert call["output_config"] == {"effort": "low"} and call["messages"] == [{"role": "user", "content": "user"}]


async def test_anthropic_refusal_is_permanent_and_invalid_output_repaired() -> None:
    with pytest.raises(LLMPermanentError):
        await AnthropicProvider(FakeAnthropic(claude_message(None, stop_reason="refusal"))).generate(
            "m", "s", "u", Out, use_search=False, thinking_level=None)
    client = FakeAnthropic(claude_message(None, text="garbage"), claude_message(Out(answer="ok")))
    r = await AnthropicProvider(client).generate("m", "s", "u", Out, use_search=False, thinking_level=None)
    assert r.data.answer == "ok" and len(client.calls) == 2


ANTHROPIC_ERRORS = [
    (anthropic_error(anthropic.OverloadedError, 529, "overloaded_error", "Overloaded"), LLMOverloadedError, None),
    (anthropic_error(anthropic.RateLimitError, 429, "rate_limit_error",
                     "This request would exceed the rate limit for your organization of 50 requests per minute.",
                     {"retry-after": "12"}), LLMTransientError, 12.0),
    (anthropic_error(anthropic.EnforcedSpendLimitReachedError, 429, "rate_limit_error",
                     "You have reached your specified API usage limits."), LLMQuotaExhaustedError, None),
    (anthropic_error(anthropic.AuthenticationError, 401, "authentication_error", "invalid x-api-key"), LLMAuthError, None),
    (anthropic_error(anthropic.NotFoundError, 404, "not_found_error", "model: claude-nope"), LLMModelNotFoundError, None),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
     LLMTimeoutError, None),
]


@pytest.mark.parametrize(("error", "expected", "retry_after"), ANTHROPIC_ERRORS,
                         ids=["overloaded-529", "rate-limit", "spend-limit", "auth", "model-not-found", "timeout"])
async def test_anthropic_error_mapping(error: Exception, expected: type, retry_after: float | None) -> None:
    with pytest.raises(expected) as exc:
        await AnthropicProvider(FakeAnthropic(error)).generate("m", "s", "u", Out, use_search=False, thinking_level=None)
    assert type(exc.value) is expected and exc.value.retry_after_seconds == retry_after


# ------------------------------------------------------------------------------ Gemini + shared helpers

async def test_gemini_provider_is_single_model_and_tags_provider() -> None:
    stub = ModelStub(**{"gemini-3-dot-5-flash": [api_error(OVERLOADED)], "gemini-3-dot-5-flash-lite": [ok()]})
    p = GeminiProvider(get_settings(), sdk_client=stub)
    r = await p.generate("gemini-3.5-flash-lite", "s", "u", Out, use_search=False, thinking_level="low")
    assert (r.provider, r.model, r.data.answer) == ("gemini", "gemini-3.5-flash-lite", "ok")
    with pytest.raises(LLMOverloadedError):  # no internal fallback: that is the router's job now
        await p.generate("gemini-3.5-flash", "s", "u", Out, use_search=False, thinking_level=None)
    assert stub.calls == ["gemini-3.5-flash-lite", "gemini-3.5-flash"]


@pytest.mark.parametrize(("status", "message", "expected"), [
    (400, "bad", LLMPermanentError), (401, "x", LLMAuthError), (403, "x", LLMAuthError),
    (404, "x", LLMModelNotFoundError), (408, "x", LLMTimeoutError), (429, "slow down", LLMTransientError),
    (429, "insufficient_quota: check your plan and billing", LLMQuotaExhaustedError), (500, "x", LLMTransientError),
    (503, "x", LLMOverloadedError), (529, "x", LLMOverloadedError)])
def test_status_error_table(status: int, message: str, expected: type) -> None:
    assert type(status_error("p", status, message)) is expected


# ------------------------------------------------------------------------------ registry

async def test_registry_registers_only_configured_providers(monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    async def down(url: str) -> bool:
        return False

    monkeypatch.setattr(registry, "ollama_reachable", down)
    s = get_settings().model_copy(update={"gemini_api_key": SecretStr("g"), "anthropic_api_key": None,
                                          "openai_api_key": None})
    with caplog.at_level("INFO"):
        providers = await registry.build_providers(s)
    assert set(providers) == {"gemini"}
    skipped = [r.getMessage() for r in caplog.records if "provider.skipped" in r.getMessage()]
    assert len(skipped) == 3 and not any("g" == part for m in skipped for part in m.split())

    async def up(url: str) -> bool:
        return True

    monkeypatch.setattr(registry, "ollama_reachable", up)
    s2 = s.model_copy(update={"anthropic_api_key": SecretStr("a"), "openai_api_key": SecretStr("o")})
    assert set(await registry.build_providers(s2)) == {"gemini", "anthropic", "openai", "ollama"}
    ollama = (await registry.build_providers(s2))["ollama"]
    assert ollama.name == "ollama"
