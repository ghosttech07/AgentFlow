"""ModelRouter (Part 8): per-role routes, cross-provider fallback, breaker, quotas, usage rows."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.llm import quota
from app.llm.base import LLMResponse
from app.llm.circuit import CircuitBreaker
from app.llm.errors import (
    LLMAuthError,
    LLMModelNotFoundError,
    LLMOutputError,
    LLMOverloadedError,
    LLMQuotaExhaustedError,
    LLMTimeoutError,
    LLMTransientError,
)
from app.llm.pricing import Pricing
from app.llm.router import ModelRouter
from app.llm.routes import QuotaSpec, RouteConfig
from app.models import LLMCall


class Out(BaseModel):
    answer: str


class StubProvider:
    """Scripted adapter: each call pops the next result for that model (an exception or an answer)."""

    def __init__(self, name: str, **script: list[Any]) -> None:
        self.name = name
        self.script = script
        self.calls: list[str] = []

    async def generate(self, model: str, system_prompt: str, user_prompt: str, output_model: type[BaseModel],
                       *, use_search: bool, thinking_level: str | None) -> LLMResponse:
        self.calls.append(model)
        queue = self.script[model]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, BaseException):
            raise result
        return LLMResponse(data=output_model(answer=result), raw_text="{}", model=model, input_tokens=1000,
                           output_tokens=500, latency_ms=5, provider=self.name)


ROUTES = RouteConfig(routes={"default": ["gemini:g", "anthropic:c", "ollama:o"], "planner": ["anthropic:c"]})
PRICES = Pricing({"gemini:g": (1.0, 2.0), "anthropic:c": (2.0, 10.0)})


def router(sf: async_sessionmaker[AsyncSession] | None, routes: RouteConfig = ROUTES, **providers: StubProvider) -> ModelRouter:
    return ModelRouter(providers=providers, routes=routes, pricing=PRICES, settings=get_settings(),
                       session_factory=sf, breaker=CircuitBreaker(2, 60))


async def test_role_route_and_default_route(session_factory: async_sessionmaker[AsyncSession]) -> None:
    g, c = StubProvider("gemini", g=["from gemini"]), StubProvider("anthropic", c=["from claude"])
    r = router(session_factory, gemini=g, anthropic=c)
    planned = await r.generate_structured("s", "u", Out, role="planner")
    assert (planned.provider, planned.model, planned.data.answer) == ("anthropic", "c", "from claude")
    written = await r.generate_structured("s", "u", Out, role="writer")  # no writer route -> default
    assert (written.provider, written.route_position, written.fallback_from) == ("gemini", 0, None)
    assert written.cost_usd == pytest.approx(0.002)  # 1000*1 + 500*2 per 1M


async def test_cross_provider_fallback_on_overload_records_metrics(session_factory: async_sessionmaker[AsyncSession]) -> None:
    g = StubProvider("gemini", g=[LLMOverloadedError("503 high demand")])
    c = StubProvider("anthropic", c=["claude saved it"])
    resp = await router(session_factory, gemini=g, anthropic=c).generate_structured("s", "u", Out, role="writer")
    assert (resp.provider, resp.route_position, resp.fallback_from) == ("anthropic", 1, "gemini:g")
    assert resp.cost_usd == pytest.approx(0.007)
    async with session_factory() as s:
        rows = (await s.scalars(select(LLMCall).order_by(LLMCall.id))).all()
    assert [(r.provider, r.outcome, r.error_type, r.route_position) for r in rows] == [
        ("gemini", "failed", "overloaded", 0), ("anthropic", "succeeded", None, 1)]
    assert rows[1].fallback_from == "gemini:g" and rows[1].input_tokens == 1000 and rows[1].role == "writer"


@pytest.mark.parametrize("error", [LLMQuotaExhaustedError("daily quota"), LLMModelNotFoundError("404"),
                                   LLMTimeoutError("slow")], ids=["quota", "not-found", "timeout"])
async def test_other_fallback_triggers(error: Exception) -> None:
    g, c = StubProvider("gemini", g=[error]), StubProvider("anthropic", c=["ok"])
    assert (await router(None, gemini=g, anthropic=c).generate_structured("s", "u", Out)).provider == "anthropic"


@pytest.mark.parametrize("error", [LLMOutputError("bad json after repair"), LLMAuthError("401 bad key"),
                                   LLMTransientError("500")], ids=["output", "auth", "transient"])
async def test_no_fallback_on_output_auth_or_plain_transient(error: Exception, caplog: pytest.LogCaptureFixture) -> None:
    g, c = StubProvider("gemini", g=[error]), StubProvider("anthropic", c=["never"])
    with caplog.at_level("ERROR"), pytest.raises(type(error)):
        await router(None, gemini=g, anthropic=c).generate_structured("s", "u", Out)
    assert c.calls == []
    if isinstance(error, LLMAuthError):
        assert any("misconfiguration" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


async def test_open_circuit_is_skipped() -> None:
    g = StubProvider("gemini", g=[LLMOverloadedError("503")])
    c = StubProvider("anthropic", c=["ok"])
    r = router(None, gemini=g, anthropic=c)
    for _ in range(2):  # threshold 2 -> circuit for gemini:g opens
        await r.generate_structured("s", "u", Out)
    await r.generate_structured("s", "u", Out)
    assert g.calls == ["g", "g"] and len(c.calls) == 3


async def test_exhausted_quota_is_routed_around_and_quota_error_marks_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    routes = RouteConfig(routes=ROUTES.routes, quotas={"gemini:g": QuotaSpec(requests_per_day=1)})
    g = StubProvider("gemini", g=["first", LLMQuotaExhaustedError("per day")])
    c = StubProvider("anthropic", c=["claude"])
    r = router(session_factory, routes, gemini=g, anthropic=c)
    assert (await r.generate_structured("s", "u", Out)).provider == "gemini"
    assert (await r.generate_structured("s", "u", Out)).provider == "anthropic"  # quota of 1 used up
    assert g.calls == ["g"]

    routes2 = RouteConfig(routes={"default": ["anthropic:c2", "gemini:g"]})
    c2 = StubProvider("anthropic", c2=[LLMQuotaExhaustedError("spend limit"), "never again"])
    g2 = StubProvider("gemini", g=["gemini answers"])
    r2 = router(session_factory, routes2, anthropic=c2, gemini=g2)
    assert (await r2.generate_structured("s", "u", Out)).provider == "gemini"
    usage = await quota.usage_now(session_factory, "anthropic:c2", None, get_settings())
    assert usage["day"]["exhausted"] is True
    await r2.generate_structured("s", "u", Out)
    assert c2.calls == ["c2"]  # never tried again in this window


async def test_all_fail_raises_overloaded_if_any_overloaded() -> None:
    g = StubProvider("gemini", g=[LLMOverloadedError("503")])
    c = StubProvider("anthropic", c=[LLMModelNotFoundError("404")])
    o = StubProvider("ollama", o=[LLMTimeoutError("slow")])
    with pytest.raises(LLMOverloadedError):
        await router(None, gemini=g, anthropic=c, ollama=o).generate_structured("s", "u", Out)
    g2 = StubProvider("gemini", g=[LLMModelNotFoundError("404")])
    c2 = StubProvider("anthropic", c=[LLMQuotaExhaustedError("quota")])
    o2 = StubProvider("ollama", o=[LLMTimeoutError("slow")])
    with pytest.raises(LLMTimeoutError):  # none overloaded -> the last error
        await router(None, gemini=g2, anthropic=c2, ollama=o2).generate_structured("s", "u", Out)


async def test_unconfigured_provider_in_legacy_route_fails_like_before() -> None:
    r = router(None, RouteConfig(routes={"default": ["gemini:g"]}))
    with pytest.raises(LLMAuthError, match="not configured"):
        await r.generate_structured("s", "u", Out)
