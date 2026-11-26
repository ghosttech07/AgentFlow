"""Legacy Gemini-only client (Parts 2-5): [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS] with a per-model
circuit breaker. Kept for backward compatibility and its tests; the engine now uses app.llm.router,
which does the same across providers. Each model call is app.llm.providers.gemini.GeminiProvider."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.llm.base import LLMResponse
from app.llm.circuit import CircuitBreaker
from app.llm.errors import LLMError, LLMModelNotFoundError, LLMOverloadedError
from app.llm.providers.gemini import GeminiProvider, _thinking_supported

logger = logging.getLogger(__name__)


class GeminiClient:
    """Create once per process. `sdk_client` is injectable for tests."""

    def __init__(
        self,
        settings: Settings,
        sdk_client: Any | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings
        self.provider = GeminiProvider(settings, sdk_client)
        self.model = settings.gemini_model  # primary
        self.models = [settings.gemini_model,
                       *[m for m in settings.gemini_fallback_models if m != settings.gemini_model]]
        self.breaker = breaker or CircuitBreaker(
            settings.model_circuit_failure_threshold, settings.model_circuit_cooldown_seconds
        )
        self.thinking_supported = _thinking_supported(self.model)
        if not self.thinking_supported:  # client is a singleton, so this logs once per process
            logger.info("llm.thinking_level_unsupported model=%s (setting ignored)", self.model)

    def grounded_json_supported(self, model: str | None = None) -> bool:
        return self.provider.grounded_json_supported(model or self.model)

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        use_search: bool = False,
        thinking_level: str | None = None,
        model: str | None = None,
        role: str | None = None,
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
                response = await self.provider.generate(
                    model, system_prompt, user_prompt, output_model, use_search=use_search,
                    thinking_level=thinking_level,
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


def _next_after(chain: list[str], model: str) -> str | None:
    i = chain.index(model)
    return chain[i + 1] if i + 1 < len(chain) else None
