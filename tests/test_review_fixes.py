"""Regression tests for the Part 8 whole-branch review findings (I-1 .. I-7)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.llm.circuit import CircuitBreaker
from app.llm.context import CallContext, use_context
from app.llm.errors import (
    BudgetPause,
    LLMOverloadedError,
    LLMQuotaExhaustedError,
)
from app.llm.pricing import Pricing
from app.llm.providers.base import status_error
from app.llm.router import ModelRouter
from app.llm.routes import QuotaSpec, RouteConfig, legacy_routes
from app.models import LLMCall, Workflow
from tests.engine_helpers import detail
from tests.test_budgets import fake_router, run_goal
from tests.test_router import Out, StubProvider


def router(sf, routes: RouteConfig, breaker: CircuitBreaker | None = None, pricing: Pricing | None = None,  # noqa: ANN001
           settings=None, **providers: StubProvider) -> ModelRouter:  # noqa: ANN001
    return ModelRouter(providers=providers, routes=routes, pricing=pricing or Pricing({}),
                       settings=settings or get_settings(), session_factory=sf,
                       breaker=breaker or CircuitBreaker(3, 60))


# --------------------------------------------------------------------------- I-1
async def test_budget_reached_exactly_on_the_last_call_completes(app: FastAPI, client: AsyncClient, make_engine,
                                                                  session_factory) -> None:  # noqa: ANN001
    r, _ = fake_router(session_factory)
    wf_id = await run_goal(app, client, r, {"max_llm_calls": 6})  # planner + 5 tasks = exactly 6 calls
    await make_engine(r).run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "COMPLETED" and wf["usage"]["llm_calls"] == 6


# --------------------------------------------------------------------------- I-2
async def test_all_entries_quota_blocked_raises_quota_exhausted(session_factory) -> None:  # noqa: ANN001
    routes = RouteConfig(routes={"default": ["gemini:g"]}, quotas={"gemini:g": QuotaSpec(requests_per_day=1)})
    r = router(session_factory, routes, gemini=StubProvider("gemini", g=["ok"]))
    await r.generate_structured("s", "u", Out)
    with pytest.raises(LLMQuotaExhaustedError):  # not "overloaded": retrying minutes later won't help
        await r.generate_structured("s", "u", Out)


async def test_legacy_route_keeps_v1_rules(session_factory) -> None:  # noqa: ANN001
    s = get_settings().model_copy(update={"gemini_model": "g1", "gemini_fallback_models": ["g2"],
                                          "gemini_worker_model": None})
    legacy = legacy_routes(s)
    assert legacy.legacy and legacy.quotas["gemini:g1"].timezone == "America/Los_Angeles"
    g = StubProvider("gemini", g1=[LLMQuotaExhaustedError("per day")], g2=["never"])
    with pytest.raises(LLMQuotaExhaustedError):  # v1 never fell back on a quota error
        await router(session_factory, legacy, settings=s, gemini=g).generate_structured("s", "u", Out)
    assert g.calls == ["g1"]


# --------------------------------------------------------------------------- I-3
async def test_half_open_trial_answered_with_quota_releases_the_circuit() -> None:
    clock = [1000.0]
    breaker = CircuitBreaker(1, 10, clock=lambda: clock[0])
    routes = RouteConfig(routes={"default": ["gemini:g", "anthropic:c"]})
    g = StubProvider("gemini", g=[LLMOverloadedError("503"), LLMQuotaExhaustedError("quota"), "back"])
    r = router(None, routes, breaker=breaker, gemini=g, anthropic=StubProvider("anthropic", c=["ok"]))
    await r.generate_structured("s", "u", Out)  # overload -> circuit open
    clock[0] += 11  # cooldown over: half-open, one trial allowed
    await r.generate_structured("s", "u", Out)  # trial answered with quota_exhausted: provider is up
    assert breaker.state("gemini:g") == "closed"
    assert (await r.generate_structured("s", "u", Out)).provider == "gemini"


async def test_budget_pause_does_not_consume_a_half_open_trial(session_factory) -> None:  # noqa: ANN001
    clock = [1000.0]
    breaker = CircuitBreaker(1, 10, clock=lambda: clock[0])
    breaker.record_failure("gemini:g")
    clock[0] += 11
    async with session_factory() as s:
        wf = Workflow(id=uuid.uuid4(), name="w", goal="g", status="RUNNING", budget={"max_llm_calls": 1})
        s.add(wf)
        await s.flush()  # the workflow row must exist before its usage row (no ORM relationship orders them)
        s.add(LLMCall(workflow_id=wf.id, role="writer", provider="gemini", model="g", outcome="succeeded"))
        await s.commit()
    r = router(session_factory, RouteConfig(routes={"default": ["gemini:g"]}), breaker=breaker,
               gemini=StubProvider("gemini", g=["x"]))
    with use_context(CallContext(wf.id, uuid.uuid4())), pytest.raises(BudgetPause):
        await r.generate_structured("s", "u", Out, role="writer")
    assert breaker.allow("gemini:g")  # the trial is still available


# --------------------------------------------------------------------------- I-4
def test_connection_refused_is_a_fallback_trigger() -> None:
    import httpx2
    import openai

    from app.llm.providers.openai_provider import OpenAICompatProvider

    err = OpenAICompatProvider("ollama", None)._map(
        openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:11434/v1")))
    assert isinstance(err, LLMOverloadedError)  # unreachable = unavailable: try the next entry


# --------------------------------------------------------------------------- I-5
async def test_cancelled_call_is_recorded(session_factory) -> None:  # noqa: ANN001
    class Hang(StubProvider):
        async def generate(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            await asyncio.Event().wait()

    r = router(session_factory, RouteConfig(routes={"default": ["gemini:g"]}), gemini=Hang("gemini"))
    call = asyncio.create_task(r.generate_structured("s", "u", Out, role="writer"))
    await asyncio.sleep(0.2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    async with session_factory() as s:
        rows = (await s.scalars(select(LLMCall))).all()
    assert [(c.provider, c.outcome) for c in rows] == [("gemini", "cancelled")]


def test_startup_warns_when_task_timeout_leaves_no_room_for_a_fallback(caplog: pytest.LogCaptureFixture) -> None:
    from app.llm.router import check_timeouts

    s = get_settings().model_copy(update={"task_timeout_seconds": 180, "llm_timeout_seconds": 120,
                                          "gemini_timeout_seconds": 60})
    with caplog.at_level("WARNING"):
        check_timeouts(s, RouteConfig(routes={"default": ["ollama:o", "gemini:g"]}))
    assert any("timeout" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- I-6
async def test_unpriced_paid_entry_pauses_when_cost_limit_is_set(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        wf = Workflow(id=uuid.uuid4(), name="w", goal="g", status="RUNNING", budget={"max_cost_usd": 1.0})
        s.add(wf)
        await s.commit()
    o = StubProvider("openai", gx=["never"])
    r = router(session_factory, RouteConfig(routes={"default": ["openai:gx"]}), openai=o)
    with use_context(CallContext(wf.id, uuid.uuid4())), pytest.raises(BudgetPause):
        await r.generate_structured("s", "u", Out, role="writer")
    assert o.calls == []
    async with session_factory() as s:
        assert (await s.get(Workflow, wf.id)).status == "PAUSED"


# --------------------------------------------------------------------------- I-7
@pytest.mark.parametrize(("status", "message"), [
    (400, "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."),
    (402, "Payment required: billing hard limit reached"),
])
def test_billing_errors_are_quota_exhausted(status: int, message: str) -> None:
    assert isinstance(status_error("anthropic", status, message), LLMQuotaExhaustedError)
