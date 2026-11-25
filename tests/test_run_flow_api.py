from __future__ import annotations

import time

from fastapi import FastAPI
from httpx import AsyncClient

from app.agents.planner import Plan
from app.llm import get_llm_client
from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import detail, started_workflow, task
from tests.test_planner import CYCLIC, GOOD


async def test_run_true_returns_fast_then_engine_plans_and_runs(
    app: FastAPI, client: AsyncClient, make_engine  # noqa: ANN001
) -> None:
    llm = FakeLLMClient({Plan: GOOD}, delay=0.2)  # every LLM call is slow
    app.dependency_overrides[get_llm_client] = lambda: llm

    t0 = time.perf_counter()
    resp = await client.post("/workflows", json={
        "name": "EV", "goal": "Compare EVs", "run": True, "constraints": {"max_tasks": 6},
    })
    assert resp.status_code == 202, resp.text
    assert time.perf_counter() - t0 < 1.0 and llm.calls == []  # no LLM call in the request
    body = resp.json()
    assert body["status"] == "PENDING" and body["run_requested"] is True and body["tasks"] == []

    assert (await client.get(f"/workflows/{body['id']}/result")).status_code == 409

    await make_engine(llm).run_until_idle()

    wf = await detail(client, body["id"])
    assert wf["status"] == "COMPLETED" and wf["run_requested"] is False
    assert wf["progress"]["succeeded"] == 5
    assert '"max_tasks": 6' in llm.calls[0]["user_prompt"]  # stored constraints reached the planner
    result = (await client.get(f"/workflows/{body['id']}/result")).json()
    assert list(result["final_outputs"]) == ["review_report"]
    assert result["summary"] == GOOD["plan_summary"]


async def test_run_true_with_tasks_starts_directly(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    resp = await client.post("/workflows", json={"name": "x", "goal": "g", "run": True,
                                                 "tasks": [task("a")]})
    assert resp.status_code == 202
    await make_engine(FakeLLMClient()).run_until_idle()
    assert (await detail(client, resp.json()["id"]))["status"] == "COMPLETED"


async def test_run_true_planning_failure(app: FastAPI, client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    llm = FakeLLMClient({Plan: [CYCLIC] * 3})
    app.dependency_overrides[get_llm_client] = lambda: llm
    resp = await client.post("/workflows", json={"name": "x", "goal": "g", "run": True})
    await make_engine(llm).run_until_idle()
    wf = await detail(client, resp.json()["id"])
    assert wf["status"] == "FAILED" and wf["run_requested"] is False
    assert "Dependency cycle" in wf["error"]


async def test_start_409_cases(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    unplanned = (await client.post("/workflows", json={"name": "x", "goal": "g"})).json()["id"]
    resp = await client.post(f"/workflows/{unplanned}/start")
    assert resp.status_code == 409 and "no tasks yet" in resp.json()["error"]["message"]

    running = await started_workflow(client, [task("a")])
    resp = await client.post(f"/workflows/{running}/start")
    assert resp.status_code == 409 and "already running" in resp.json()["error"]["message"]
    assert (await client.get(f"/workflows/{running}/result")).status_code == 409

    await make_engine(FakeLLMClient()).run_until_idle()
    resp = await client.post(f"/workflows/{running}/start")
    assert resp.status_code == 409 and "terminal" in resp.json()["error"]["message"]
    assert (await client.get(f"/workflows/{running}/result")).status_code == 200

    missing = await client.post("/workflows/00000000-0000-0000-0000-000000000000/start")
    assert missing.status_code == 404


async def test_engine_status(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    disabled = (await client.get("/engine/status")).json()
    assert disabled["enabled"] is False
    engine = make_engine(FakeLLMClient(), engine_max_concurrency=3)
    await engine.tick()
    status = (await client.get("/engine/status")).json()
    assert status["enabled"] is True and status["worker_id"] == "test-worker"
    assert status["max_concurrency"] == 3 and status["in_flight_task_ids"] == []
    assert status["last_tick_at"] is not None
