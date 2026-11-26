"""Anthropic Claude via the official `anthropic` SDK.

Structured output: native `messages.parse(output_format=<Pydantic model>)` (constrained decoding,
`parsed_output` validated by the SDK). thinking_level maps to `output_config.effort`; current
Claude models think adaptively, so no `thinking` parameter is sent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from app.llm.base import LLMResponse
from app.llm.errors import (
    LLMError,
    LLMOutputError,
    LLMOverloadedError,
    LLMPermanentError,
    LLMQuotaExhaustedError,
    LLMTimeoutError,
)
from app.llm.providers.base import EFFORT, parse_json, repair_prompt, retry_after_header, status_error

logger = logging.getLogger(__name__)

MAX_TOKENS = 16000  # non-streaming ceiling that stays under SDK HTTP timeouts
# Spend / billing limits: waiting minutes will not help, so these are quota_exhausted (route around).
_SPEND_LIMIT_ERRORS = tuple(
    cls for cls in (getattr(anthropic, n, None) for n in (
        "EnforcedSpendLimitReachedError", "ContractSpendLimitReachedError",
        "LinkedOrganizationSpendLimitReachedError", "PaymentRequiredError"))
    if isinstance(cls, type)
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any, timeout: float | None = None) -> None:
        self._client = client
        self._timeout = timeout

    async def generate(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        *,
        use_search: bool,
        thinking_level: str | None,
    ) -> LLMResponse:
        if use_search:
            logger.debug("llm.search_ignored provider=anthropic (web search is a tool from Part 9)")
        started = time.perf_counter()
        usage = [0, 0]
        try:
            data, raw = await self._call(model, system_prompt, user_prompt, output_model, thinking_level, usage)
        except LLMOutputError as err:  # exactly one repair attempt
            logger.warning("llm.repair_attempt provider=anthropic model=%s reason=%s", model, err.message[:200])
            prompt = repair_prompt(user_prompt, getattr(err, "raw", ""), err.message)
            data, raw = await self._call(model, system_prompt, prompt, output_model, thinking_level, usage)
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info("llm.generate provider=anthropic model=%s output=%s latency_ms=%d input_tokens=%d "
                    "output_tokens=%d", model, output_model.__name__, latency_ms, usage[0], usage[1])
        return LLMResponse(data=data, raw_text=raw, model=model, input_tokens=usage[0], output_tokens=usage[1],
                           latency_ms=latency_ms, provider=self.name)

    async def _call(self, model: str, system_prompt: str, user_prompt: str, output_model: type[BaseModel],
                    thinking_level: str | None, usage: list[int]) -> tuple[BaseModel, str]:
        kwargs: dict[str, Any] = {
            "model": model, "max_tokens": MAX_TOKENS, "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}], "output_format": output_model,
        }
        if effort := EFFORT.get(thinking_level or ""):
            kwargs["output_config"] = {"effort": effort}
        try:
            request = self._client.messages.parse(**kwargs)
            resp = await (asyncio.wait_for(request, self._timeout) if self._timeout else request)
        except ValidationError as exc:
            raise LLMOutputError(f"anthropic output failed validation: {exc.error_count()} errors") from exc
        except Exception as exc:
            raise self._map(exc) from exc

        if getattr(resp, "usage", None) is not None:
            usage[0] += resp.usage.input_tokens or 0
            usage[1] += resp.usage.output_tokens or 0
        if resp.stop_reason == "refusal":
            raise LLMPermanentError("anthropic declined the request (stop_reason=refusal)")
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if resp.parsed_output is not None:
            return resp.parsed_output, raw
        try:
            return parse_json(raw, output_model, self.name), raw
        except LLMOutputError as err:
            err.raw = raw  # type: ignore[attr-defined]
            raise

    def _map(self, exc: BaseException) -> LLMError:
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, (TimeoutError, anthropic.APITimeoutError)):  # APITimeoutError before its parent
            return LLMTimeoutError("anthropic request timed out")
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMOverloadedError(f"Cannot reach anthropic: {type(exc).__name__}")  # unavailable -> fall back
        if _SPEND_LIMIT_ERRORS and isinstance(exc, _SPEND_LIMIT_ERRORS):
            return LLMQuotaExhaustedError(f"anthropic spend limit reached: {getattr(exc, 'message', exc)}",
                                          details={"provider": "anthropic"})
        if isinstance(exc, anthropic.OverloadedError):
            return LLMOverloadedError(f"anthropic overloaded: {exc.message}",
                                      retry_after_seconds=retry_after_header(exc.response.headers))
        if isinstance(exc, anthropic.APIStatusError):
            return status_error("anthropic", exc.status_code, exc.message, retry_after_header(exc.response.headers))
        return LLMPermanentError(f"Unexpected error calling anthropic: {type(exc).__name__}")
