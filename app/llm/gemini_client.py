"""Gemini implementation of LLMClient (google-genai SDK, async client)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.llm.base import LLMResponse, Source
from app.llm.circuit import CircuitBreaker
from app.llm.errors import (
    LLMError,
    LLMModelNotFoundError,
    LLMOutputError,
    LLMOverloadedError,
    LLMPermanentError,
    classify_exception,
)

logger = logging.getLogger(__name__)

BLOCKING_FINISH_REASONS = frozenset(
    {
        types.FinishReason.SAFETY,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.SPII,
        types.FinishReason.RECITATION,
    }
)


class GeminiClient:
    """Create once per process (see app.llm.get_llm_client). `sdk_client` is injectable for tests."""

    def __init__(
        self,
        settings: Settings,
        sdk_client: Any | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings
        self._sdk = sdk_client
        self.model = settings.gemini_model  # primary
        self.models = [settings.gemini_model,
                       *[m for m in settings.gemini_fallback_models if m != settings.gemini_model]]
        self.breaker = breaker or CircuitBreaker(
            settings.model_circuit_failure_threshold, settings.model_circuit_cooldown_seconds
        )
        self.thinking_supported = _thinking_supported(self.model)
        if not self.thinking_supported:  # client is a singleton, so this logs once per process
            logger.info("llm.thinking_level_unsupported model=%s (setting ignored)", self.model)

    def _client(self) -> Any:
        if self._sdk is None:
            if self._settings.gemini_api_key is None:
                raise LLMPermanentError("GEMINI_API_KEY is not configured")
            self._sdk = genai.Client(api_key=self._settings.gemini_api_key.get_secret_value())
        return self._sdk

    def grounded_json_supported(self, model: str | None = None) -> bool:
        # Gemini 3+ accepts google_search together with JSON schema output; 2.x rejects it.
        # Aliases without a version (e.g. gemini-flash-latest) use the always-safe two-step path.
        return _model_major(model or self.model) >= 3

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        use_search: bool = False,
        thinking_level: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Try each healthy model in [model or GEMINI_MODEL, *the rest of the chain].

        Only overload (503) and model-not-found move on to the next model; any other error is
        raised at once. Models with an open circuit are skipped. None usable -> LLMOverloadedError.
        """
        last_error: LLMError | None = None
        # A preferred (fast worker) model falls back to the other fallbacks first, the primary last.
        chain = list(dict.fromkeys([model, *self.models[1:], self.models[0]])) if model else self.models
        first = chain[0]
        for model in chain:  # noqa: B020 - `model` is reused as the loop variable on purpose
            if not self.breaker.allow(model):
                logger.warning("llm.model_skipped model=%s reason=circuit_open", model)
                continue
            try:
                response = await self._generate_with_model(
                    model, system_prompt, user_prompt, output_model, use_search, thinking_level
                )
            except LLMOverloadedError as exc:
                self.breaker.record_failure(model)
                last_error = exc
                logger.warning("llm.fallback model=%s reason=overloaded next=%s",
                               model, _next_after(chain, model) or "none")
                continue
            except LLMModelNotFoundError as exc:
                last_error = exc
                logger.warning("llm.fallback model=%s reason=model_not_found next=%s",
                               model, _next_after(chain, model) or "none")
                continue
            except LLMError:
                self.breaker.record_success(model)  # the model answered: it is not overloaded
                raise
            self.breaker.record_success(model)
            if model != first:
                logger.warning("llm.used_fallback model=%s primary=%s", model, first)
            return response

        if last_error is None or isinstance(last_error, LLMOverloadedError):
            reason = last_error.message if last_error else "circuit open for every model"
            raise LLMOverloadedError(
                f"All models unavailable ({', '.join(chain)}): {reason}",
                retry_after_seconds=(last_error.retry_after_seconds if last_error else None)
                or self.breaker.seconds_until_available(chain),
            )
        raise last_error


    async def _generate_with_model(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        use_search: bool,
        thinking_level: str | None,
    ) -> LLMResponse:
        start = time.perf_counter()
        thinking = thinking_level if _thinking_supported(model) else None
        usage = [0, 0]  # input, output tokens summed over every call made
        sources: list[Source] = []
        prompt = user_prompt
        search_in_json_call = use_search and self.grounded_json_supported(model)

        if use_search and not search_in_json_call:
            # Two-step: grounded free text first, then convert it to the JSON schema.
            notes, sources = await self._generate(
                model, prompt, self._config(system_prompt, search=True, thinking=thinking), usage
            )
            prompt = _with_research_notes(user_prompt, notes, sources)

        json_config = self._config(
            system_prompt, output_model=output_model, search=search_in_json_call, thinking=thinking
        )
        raw, json_sources = await self._generate(model, prompt, json_config, usage)
        sources = sources or json_sources
        try:
            data = _parse(raw, output_model)
        except LLMOutputError as err:
            logger.warning("llm.repair_attempt model=%s reason=%s", model, err.message[:200])
            raw, _ = await self._generate(
                model, _repair_prompt(prompt, raw, err.message), json_config, usage
            )
            data = _parse(raw, output_model)  # still bad -> LLMOutputError propagates

        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "llm.generate model=%s output=%s latency_ms=%d input_tokens=%d output_tokens=%d "
            "search=%s thinking=%s sources=%d",
            model, output_model.__name__, latency_ms, usage[0], usage[1], use_search,
            thinking or "default", len(sources),
        )
        return LLMResponse(
            data=data, raw_text=raw, model=model, input_tokens=usage[0],
            output_tokens=usage[1], latency_ms=latency_ms, sources=sources,
        )

    def _config(
        self,
        system_prompt: str,
        *,
        output_model: type[BaseModel] | None = None,
        search: bool = False,
        thinking: str | None = None,
    ) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            thinking_config=(
                types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking.upper()))
                if thinking else None
            ),
            system_instruction=system_prompt,
            temperature=self._settings.gemini_temperature,
            response_mime_type="application/json" if output_model else None,
            response_json_schema=output_model.model_json_schema() if output_model else None,
            tools=[types.Tool(google_search=types.GoogleSearch())] if search else None,
        )

    async def _generate(
        self, model: str, prompt: str, config: types.GenerateContentConfig, usage: list[int]
    ) -> tuple[str, list[Source]]:
        """One SDK call with timeout + error mapping. Returns (text, grounding sources)."""
        logger.debug("llm.request model=%s prompt=%s", model, prompt)
        try:
            response = await asyncio.wait_for(
                self._client().aio.models.generate_content(model=model, contents=prompt, config=config),
                timeout=self._settings.gemini_timeout_seconds,
            )
        except Exception as exc:
            raise classify_exception(exc) from exc

        meta = response.usage_metadata
        if meta is not None:
            usage[0] += meta.prompt_token_count or 0
            usage[1] += (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)

        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise LLMPermanentError(f"Prompt blocked by Gemini safety filters: {feedback.block_reason}")
        candidate = response.candidates[0] if response.candidates else None
        if candidate is not None and candidate.finish_reason in BLOCKING_FINISH_REASONS:
            raise LLMPermanentError(f"Response blocked by Gemini: {candidate.finish_reason}")

        return response.text or "", _extract_sources(candidate)


def _next_after(chain: list[str], model: str) -> str | None:
    i = chain.index(model)
    return chain[i + 1] if i + 1 < len(chain) else None


def _thinking_supported(model: str) -> bool:
    return _model_major(model) >= 3 and "thinking_level" in types.ThinkingConfig.model_fields


def _model_major(model: str) -> int:
    """Major version from names like 'gemini-3.5-flash'; 0 for aliases without one."""
    match = re.match(r"gemini-(\d+)", model)
    return int(match.group(1)) if match else 0


def _extract_sources(candidate: Any) -> list[Source]:
    meta = getattr(candidate, "grounding_metadata", None)
    sources: list[Source] = []
    seen: set[str] = set()
    for chunk in (meta.grounding_chunks or []) if meta else []:
        web = chunk.web
        if web and web.uri and web.uri not in seen:
            seen.add(web.uri)
            sources.append(Source(title=web.title or web.domain or web.uri, url=web.uri))
    return sources


def _parse(raw: str, output_model: type[BaseModel]) -> BaseModel:
    text = raw.strip()
    if text.startswith("```"):  # tolerate markdown fences
        text = text.strip("`").removeprefix("json").strip()
    if not text:
        raise LLMOutputError("Gemini returned an empty response")
    try:
        return output_model.model_validate_json(text)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:10]
        )
        raise LLMOutputError(f"Gemini output failed validation: {problems}") from exc


def _repair_prompt(prompt: str, bad_output: str, error: str) -> str:
    return (
        f"{prompt}\n\n---\nYour previous response was invalid.\n"
        f"PREVIOUS RESPONSE:\n{bad_output[:8000]}\n\nERROR:\n{error}\n\n"
        "Return ONLY the corrected JSON object that matches the required schema."
    )


def _with_research_notes(prompt: str, notes: str, sources: list[Source]) -> str:
    listed = "\n".join(f"- {s.title}: {s.url}" for s in sources) or "(none returned)"
    return (
        f"{prompt}\n\n---\nRESEARCH NOTES (from a Google-Search-grounded step):\n{notes}\n\n"
        f"SOURCES ACTUALLY USED:\n{listed}\n\n"
        "Convert these notes into the required JSON. Use only facts from the notes and "
        "only the sources listed above."
    )
