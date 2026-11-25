"""Deterministic LLMClient for tests: no network, no cost."""

from __future__ import annotations

import asyncio
import types as pytypes
from dataclasses import dataclass
import typing
from typing import Any, Literal, get_args, get_origin

from annotated_types import Ge, MinLen
from pydantic import BaseModel

from app.llm.base import LLMResponse, Source


@dataclass
class FakeScript:
    """Scripted behaviour for calls whose user prompt contains `match` (e.g. a task name).

    error: raise it; hang: never return (triggers the engine's task timeout);
    delay: seconds to sleep first; response: payload to return instead of the default;
    times: apply only to the first N matching calls (None = always).
    """

    match: str
    error: Exception | None = None
    hang: bool = False
    delay: float = 0.0
    response: dict[str, Any] | None = None
    times: int | None = None


class FakeLLMClient:
    """Returns canned data that satisfies the requested schema.

    - `responses`: optional {OutputModel: dict | [dict, ...]} overrides; a list is consumed one
      item per call (for multi-call scenarios such as plan correction).
    - `error`: exception to raise on every call (e.g. LLMTransientError).
    - `delays`: {OutputModel: seconds} per-agent latency (each agent has its own output model);
      `delay` applies to every call.
    - `scripts`: per-task behaviour, matched by substring of the user prompt (see FakeScript).
    - `calls`: every request is recorded for prompt assertions.
    """

    def __init__(
        self,
        responses: dict[type[BaseModel], dict[str, Any] | list[dict[str, Any]]] | None = None,
        error: Exception | None = None,
        sources: list[Source] | None = None,
        delay: float = 0.0,
        delays: dict[type[BaseModel], float] | None = None,
        scripts: list[FakeScript] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.error = error
        self.sources = sources or []
        self.delay = delay
        self.delays = delays or {}
        self.scripts = scripts or []
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        use_search: bool = False,
        thinking_level: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt,
             "output_model": output_model, "use_search": use_search,
             "thinking_level": thinking_level, "model": model}
        )
        script = next((s for s in self.scripts
                       if s.match in user_prompt and (s.times is None or s.times > 0)), None)
        if script is not None and script.times is not None:
            script.times -= 1
        await asyncio.sleep(self.delay + self.delays.get(output_model, 0.0) + (script.delay if script else 0))
        if script and script.hang:
            await asyncio.Event().wait()  # never set
        if script and script.error is not None:
            raise script.error
        if self.error is not None:
            raise self.error
        payload = (
            (script.response if script else None)
            or self.responses.get(output_model)
            or fake_data(output_model)
        )
        if isinstance(payload, list):
            payload = payload.pop(0)
        if callable(payload):  # dynamic canned output, e.g. built from the goal in the prompt
            payload = payload(user_prompt)
        data = output_model.model_validate(payload)
        return LLMResponse(
            data=data, raw_text=data.model_dump_json(), model="fake-model",
            input_tokens=len(user_prompt) // 4, output_tokens=10, latency_ms=1,
            sources=list(self.sources),
        )


def fake_data(model: type[BaseModel]) -> dict[str, Any]:
    """Minimal valid payload for `model`, derived from its field types and constraints."""
    return {name: _fake_value(f.annotation, f.metadata) for name, f in model.model_fields.items()}


def _fake_value(annotation: Any, metadata: list[Any]) -> Any:
    origin = get_origin(annotation)
    if origin in (typing.Union, pytypes.UnionType):  # Optional[X] -> X
        annotation = next(a for a in get_args(annotation) if a is not type(None))
        origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin is list:
        count = max([m.min_length for m in metadata if isinstance(m, MinLen)] or [1])
        return [_fake_value(get_args(annotation)[0], []) for _ in range(count)]
    if origin is dict:
        return {}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return fake_data(annotation)
    if annotation is bool:
        return True
    if annotation is int:
        return max([m.ge for m in metadata if isinstance(m, Ge)] or [1])
    if annotation is float:
        return 0.5
    return "fake text"
