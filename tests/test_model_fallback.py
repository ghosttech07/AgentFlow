"""Model fallback chain + per-model circuit breaker (stub SDK, fake clock; no network)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.config import get_settings
from app.llm.circuit import CircuitBreaker
from app.llm.errors import LLMOverloadedError, LLMPermanentError
from app.llm.gemini_client import GeminiClient
from tests.test_errors_classification import BAD_KEY, MODEL_NOT_FOUND, OVERLOADED, api_error

PRIMARY, BACKUP = "gemini-3.5-flash", "gemini-3.5-flash-lite"


class Out(BaseModel):
    answer: str


def ok(text: str = '{"answer": "ok"}') -> SimpleNamespace:
    return SimpleNamespace(text=text, prompt_feedback=None, candidates=[],
                           usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=3,
                                                          thoughts_token_count=0))


class ModelStub:
    """Per-model scripted results; records which model each call went to."""

    def __init__(self, **by_model: list[Any]) -> None:
        self.by_model = {k.replace("_", "-").replace("-dot-", "."): v for k, v in by_model.items()}
        self.calls: list[str] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, *, model: str, **_: Any) -> Any:
        self.calls.append(model)
        await asyncio.sleep(0)
        queue = self.by_model[model]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, BaseException):
            raise result
        return result


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def client(stub: ModelStub, breaker: CircuitBreaker | None = None) -> GeminiClient:
    settings = get_settings().model_copy(update={"gemini_model": PRIMARY, "gemini_fallback_models": [BACKUP]})
    return GeminiClient(settings, sdk_client=stub, breaker=breaker)


def stub(primary: list[Any], backup: list[Any]) -> ModelStub:
    s = ModelStub()
    s.by_model = {PRIMARY: primary, BACKUP: backup}
    return s


async def test_overloaded_primary_falls_back_and_records_model() -> None:
    s = stub([api_error(OVERLOADED)], [ok()])
    result = await client(s).generate_structured("sys", "user", Out)
    assert s.calls == [PRIMARY, BACKUP]
    assert result.model == BACKUP and result.data == Out(answer="ok")


async def test_model_not_found_falls_back() -> None:
    s = stub([api_error(MODEL_NOT_FOUND)], [ok()])
    result = await client(s).generate_structured("sys", "user", Out)
    assert result.model == BACKUP


async def test_non_overload_error_does_not_fall_back() -> None:
    s = stub([api_error(BAD_KEY)], [ok()])
    with pytest.raises(LLMPermanentError):
        await client(s).generate_structured("sys", "user", Out)
    assert s.calls == [PRIMARY]


async def test_all_overloaded_raises_overloaded() -> None:
    s = stub([api_error(OVERLOADED)], [api_error(OVERLOADED)])
    with pytest.raises(LLMOverloadedError, match="All models unavailable"):
        await client(s).generate_structured("sys", "user", Out)
    assert s.calls == [PRIMARY, BACKUP]


async def test_circuit_opens_skips_model_then_half_open() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60, clock=clock)
    s = stub([api_error(OVERLOADED)], [ok()])
    c = client(s, breaker)

    for _ in range(2):  # two consecutive overloads -> primary circuit opens
        await c.generate_structured("sys", "user", Out)
    assert breaker.state(PRIMARY) == "open"
    s.calls.clear()

    result = await c.generate_structured("sys", "user", Out)
    assert s.calls == [BACKUP] and result.model == BACKUP  # primary skipped while open

    clock.now += 61  # cooldown over -> half-open: exactly one trial call to the primary
    assert breaker.state(PRIMARY) == "half_open"
    s.by_model[PRIMARY] = [ok('{"answer": "primary back"}')]
    s.calls.clear()
    result = await c.generate_structured("sys", "user", Out)
    assert s.calls == [PRIMARY] and result.model == PRIMARY
    assert breaker.state(PRIMARY) == "closed"


def test_half_open_allows_one_trial_and_reopens_on_failure() -> None:
    clock = Clock()
    b = CircuitBreaker(threshold=1, cooldown_seconds=10, clock=clock)
    b.record_failure("m")
    assert not b.allow("m")
    clock.now += 10
    assert b.allow("m") and not b.allow("m")  # only one trial in flight
    b.record_failure("m")  # trial failed -> open again for a full cooldown
    assert b.state("m") == "open"
    assert b.seconds_until_available(["m"]) == pytest.approx(10)


async def test_all_circuits_open_raises_without_calling() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure(PRIMARY)
    breaker.record_failure(BACKUP)
    s = stub([ok()], [ok()])
    with pytest.raises(LLMOverloadedError) as info:
        await client(s, breaker).generate_structured("sys", "user", Out)
    assert s.calls == []
    assert info.value.retry_after_seconds == pytest.approx(30)
