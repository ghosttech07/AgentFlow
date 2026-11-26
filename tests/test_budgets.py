"""Workflow budgets (Part 8): every limit pauses (never fails), planner calls count, PATCH + resume."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.planner import Plan
from app.config import get_settings
from app.llm import get_llm_client
from app.llm.circuit import CircuitBreaker
from app.llm.fake_client import FakeLLMClient
from app.llm.pricing import Pricing
from app.llm.providers.fake_provider import FakeProvider
from app.llm.router import ModelRouter
from app.llm.routes import RouteConfig
from app.models import LLMCall
from tests.control_helpers import control, events
from tests.engine_helpers import detail, tasks_by_key
from tests.reliability_helpers import attempts, outcomes
from tests.test_planner import GOOD


def fake_router(sf: async_sessionmaker[AsyncSession], price: tuple[float, float] | None = None,
                llm: FakeLLMClient | None = None) -> tuple[ModelRouter, FakeLLMClient]:
    """Router over the fake client. With `price`, the entry is billed (a stand-in for a paid model)."""
    llm = llm or FakeLLMClient({Plan: GOOD})
    entry = "gemini:priced-fake" if price else "fake:fake-model"
    provider = entry.split(":")[0]
    router = ModelRouter(providers={provider: FakeProvider(llm)}, routes=RouteConfig(routes={"default": [entry]}),
                         pricing=Pricing({entry: price} if price else {}), settings=get_settings(),
                         session_factory=sf, breaker=CircuitBreaker(3, 60))
    return router, llm


async def run_goal(app: FastAPI, client: AsyncClient, router: ModelRouter, budget: dict[str, Any]) -> str:
    app.dependency_overrides[get_llm_client] = lambda: router
    resp = await client.post("/workflows", json={"name": "b", "goal": "Compare EVs", "run": True, "budget": budget})
    assert resp.status_code == 202, resp.text
    assert resp.json()["budget"] == budget
    return resp.json()["id"]


async def llm_call_count(sf: async_sessionmaker[AsyncSession], wf_id: str) -> int:
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.workflow_id == uuid.UUID(wf_id)))


async def test_max_llm_calls_pauses_then_patch_and_resume_completes(app: FastAPI, client: AsyncClient,
                                                                    make_engine, session_factory) -> None:  # noqa: ANN001
    router, llm = fake_router(session_factory)
    wf_id = await run_goal(app, client, router, {"max_llm_calls": 3})
    engine = make_engine(router)
    await engine.run_until_idle()

    wf = await detail(client, wf_id)
    assert wf["status"] == "PAUSED"  # paused for a human, NOT failed
    assert await llm_call_count(session_factory, wf_id) == 3  # planner (1) + two parallel researchers
    assert wf["usage"]["llm_calls"] == 3 and wf["remaining"]["llm_calls"] == 0
    exceeded = (await events(client, wf_id, "workflow.budget_exceeded"))[0]["payload"]
    assert exceeded["limit"] == "max_llm_calls" and exceeded["usage"]["llm_calls"] == 3
    paused = (await events(client, wf_id, "workflow.paused"))[0]["payload"]
    assert paused["reason"] == "budget_exceeded: max_llm_calls" and paused["mode"] == "graceful"
    tasks = await tasks_by_key(client, wf_id)
    assert tasks["compare"]["status"] == "PENDING"  # nothing new claimed while paused
    # graceful: both researchers were in flight when the limit was reached; their results are saved
    assert all(tasks[k]["status"] == "SUCCEEDED" and tasks[k]["output"] for k in ("research_india", "research_china"))
    assert tasks["research_india"]["metrics"]["provider"] == "fake" and tasks["research_india"]["metrics"]["cost_usd"] == 0

    resp = await client.patch(f"/workflows/{wf_id}/budget", json={"actor": "ops", "budget": {"max_llm_calls": 10}})
    assert resp.status_code == 200, resp.text
    upd = (await events(client, wf_id, "workflow.budget_updated"))[0]["payload"]
    assert upd["actor"] == "ops" and upd["from"] == {"max_llm_calls": 3} and upd["to"] == {"max_llm_calls": 10}
    assert (await control(client, wf_id, "resume")).json()["status"] == "RUNNING"
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert await llm_call_count(session_factory, wf_id) == 6 and len(llm.calls) == 6


async def test_max_tokens_pauses(app: FastAPI, client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    router, _ = fake_router(session_factory)
    wf_id = await run_goal(app, client, router, {"max_tokens": 1})  # the planner call alone exceeds it
    await make_engine(router).run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "PAUSED" and wf["usage"]["llm_calls"] == 1  # nothing claimed after planning
    assert (await events(client, wf_id, "workflow.budget_exceeded"))[0]["payload"]["limit"] == "max_tokens"


async def test_max_cost_pauses_after_the_call_that_reaches_it(app: FastAPI, client: AsyncClient, make_engine,
                                                              session_factory) -> None:  # noqa: ANN001
    router, _ = fake_router(session_factory, price=(1000.0, 1000.0))
    wf_id = await run_goal(app, client, router, {"max_cost_usd": 0.000001})
    await make_engine(router).run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "PAUSED" and wf["usage"]["cost_usd"] > 0.000001
    assert (await events(client, wf_id, "workflow.budget_exceeded"))[0]["payload"]["limit"] == "max_cost_usd"


async def test_pre_call_estimate_pauses_before_calling(app: FastAPI, client: AsyncClient, make_engine,
                                                       session_factory) -> None:  # noqa: ANN001
    # $1 per 1M tokens: the planner call costs ~$0.0003; a research call is estimated at
    # prompt/4 + 1500 output tokens (no history yet) = ~$0.0016, which would cross the $0.001 limit.
    router, llm = fake_router(session_factory, price=(1.0, 1.0))
    wf_id = await run_goal(app, client, router, {"max_cost_usd": 0.001})
    await make_engine(router).run_until_idle()

    wf = await detail(client, wf_id)
    assert wf["status"] == "PAUSED" and len(llm.calls) == 1  # only the planner ran
    reason = (await events(client, wf_id, "workflow.budget_exceeded"))[0]["payload"]
    assert reason["limit"] == "max_cost_usd" and reason["estimated"] is True
    t = (await tasks_by_key(client, wf_id))["research_india"]
    assert t["status"] == "READY" and (t["attempt_count"], t["max_attempts"]) == (1, 4)  # no retry used up
    assert outcomes(await attempts(client, wf_id, "research_india")) == [(1, "budget_paused", None)]


async def test_max_duration_pauses_from_the_scheduler(app: FastAPI, client: AsyncClient, make_engine,
                                                      session_factory) -> None:  # noqa: ANN001
    router, _ = fake_router(session_factory, llm=FakeLLMClient({Plan: GOOD}, delay=0.15))
    wf_id = await run_goal(app, client, router, {"max_duration_seconds": 0.1})
    await make_engine(router).run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "PAUSED"
    assert (await events(client, wf_id, "workflow.budget_exceeded"))[0]["payload"]["limit"] == "max_duration_seconds"


async def test_budget_pause_survives_restart_and_default_safety_net(app: FastAPI, client: AsyncClient,
                                                                    make_engine, session_factory) -> None:  # noqa: ANN001
    router, _ = fake_router(session_factory)
    wf_id = await run_goal(app, client, router, {"max_llm_calls": 1})
    await make_engine(router).run_until_idle()
    restarted = make_engine(router, engine_worker_id="after-restart")
    await restarted.recover()
    await restarted.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "PAUSED"

    no_budget = (await client.post("/workflows", json={"name": "d", "goal": "g"})).json()
    assert no_budget["budget"] is None and no_budget["effective_budget"]["max_llm_calls"] == 50
    assert no_budget["effective_budget"]["max_cost_usd"] is None


async def test_patch_budget_validation_and_conflicts(client: AsyncClient) -> None:
    wf_id = (await client.post("/workflows", json={"name": "d", "goal": "g"})).json()["id"]
    bad = await client.patch(f"/workflows/{wf_id}/budget", json={"actor": "x", "budget": {"max_llm_calls": -1}})
    assert bad.status_code == 422
    unknown = await client.patch(f"/workflows/{wf_id}/budget", json={"actor": "x", "budget": {"max_bananas": 1}})
    assert unknown.status_code == 422
    missing = await client.patch(f"/workflows/{uuid.uuid4()}/budget", json={"actor": "x", "budget": {}})
    assert missing.status_code == 404
    ok = await client.patch(f"/workflows/{wf_id}/budget", json={"budget": {"max_cost_usd": 2.5}})
    assert ok.status_code == 200 and ok.json()["budget"] == {"max_cost_usd": 2.5}
    await asyncio.sleep(0)
