"""OpenAI (and any OpenAI-compatible endpoint, e.g. local Ollama) via the official `openai` SDK.

Structured output: `chat.completions.parse(response_format=<Pydantic model>)` sends a strict JSON
schema and returns `message.parsed`. Chat Completions (not the Responses API) because Ollama's
OpenAI-compatible endpoint implements it, so one adapter serves both providers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import openai
from pydantic import BaseModel, ValidationError

from app.llm.base import LLMResponse
from app.llm.errors import LLMError, LLMOutputError, LLMOverloadedError, LLMPermanentError, LLMTimeoutError
from app.llm.providers.base import EFFORT, parse_json, repair_prompt, retry_after_header, status_error

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(self, name: str, client: Any, timeout: float | None = None) -> None:
        self.name = name
        self._client = client
        self._timeout = timeout
        self._no_reasoning: set[str] = set()  # models that rejected reasoning_effort (learned at runtime)

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
            logger.debug("llm.search_ignored provider=%s (web search is a tool from Part 9)", self.name)
        started = time.perf_counter()
        usage = [0, 0]
        try:
            data, raw = await self._call(model, system_prompt, user_prompt, output_model, thinking_level, usage)
        except LLMOutputError as err:  # exactly one repair attempt
            logger.warning("llm.repair_attempt provider=%s model=%s reason=%s", self.name, model, err.message[:200])
            prompt = repair_prompt(user_prompt, getattr(err, "raw", ""), err.message)
            data, raw = await self._call(model, system_prompt, prompt, output_model, thinking_level, usage)
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info("llm.generate provider=%s model=%s output=%s latency_ms=%d input_tokens=%d output_tokens=%d",
                    self.name, model, output_model.__name__, latency_ms, usage[0], usage[1])
        return LLMResponse(data=data, raw_text=raw, model=model, input_tokens=usage[0], output_tokens=usage[1],
                           latency_ms=latency_ms, provider=self.name)

    async def _call(self, model: str, system_prompt: str, user_prompt: str, output_model: type[BaseModel],
                    thinking_level: str | None, usage: list[int]) -> tuple[BaseModel, str]:
        effort = EFFORT.get(thinking_level or "") if model not in self._no_reasoning else None
        kwargs: dict[str, Any] = {
            "model": model, "response_format": output_model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        }
        if effort:
            kwargs["reasoning_effort"] = effort
        try:
            request = self._client.chat.completions.parse(**kwargs)
            resp = await (asyncio.wait_for(request, self._timeout) if self._timeout else request)
        except openai.BadRequestError as exc:
            # Non-reasoning model (OpenAI: "unsupported parameter reasoning_effort"; Ollama: "does not
            # support thinking"): remember it and resend without the parameter.
            if effort and ("reasoning_effort" in str(exc) or "support thinking" in str(exc)):
                self._no_reasoning.add(model)
                logger.info("llm.reasoning_effort_unsupported provider=%s model=%s", self.name, model)
                return await self._call(model, system_prompt, user_prompt, output_model, None, usage)
            raise self._map(exc) from exc
        except (ValidationError, json.JSONDecodeError, openai.LengthFinishReasonError) as exc:
            raise LLMOutputError(f"{self.name} output failed validation: {type(exc).__name__}") from exc
        except Exception as exc:
            raise self._map(exc) from exc

        if getattr(resp, "usage", None) is not None:
            usage[0] += resp.usage.prompt_tokens or 0
            usage[1] += resp.usage.completion_tokens or 0
        message = resp.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMPermanentError(f"{self.name} refused the request: {message.refusal}")
        raw = message.content or ""
        if message.parsed is not None:
            return message.parsed, raw
        try:
            return parse_json(raw, output_model, self.name), raw
        except LLMOutputError as err:
            err.raw = raw  # type: ignore[attr-defined]  # carried into the repair prompt
            raise

    def _map(self, exc: BaseException) -> LLMError:
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, (TimeoutError, openai.APITimeoutError)):  # APITimeoutError before its parent
            return LLMTimeoutError(f"{self.name} request timed out")
        if isinstance(exc, openai.APIConnectionError):
            # Unreachable (e.g. a stopped Ollama) = unavailable: overloaded, so the router tries the next entry.
            return LLMOverloadedError(f"Cannot reach {self.name}: {type(exc).__name__}")
        if isinstance(exc, openai.APIStatusError):
            return status_error(self.name, exc.status_code, f"{exc.message} {exc.body or ''}",
                                retry_after_header(exc.response.headers))
        return LLMPermanentError(f"Unexpected error calling {self.name}: {type(exc).__name__}")
