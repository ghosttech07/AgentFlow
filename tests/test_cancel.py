from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.agents.planner import Plan
from app.llm import get_llm_client
from app.llm.errors import LLMPermanentError
from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.control_helpers import control, events, only_pending
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, outcomes
from tests.test_planner import GOOD


async def _in_state(client: AsyncClient, make_engine, state: str) -> str:  # noqa: ANN001
    if state == "PENDING":
        return (await client.post("/workflows", json={"name": "x", "goal": "g", "tasks": [task("a")]})).json()["id"]
    if state == "AWAITING_APPROVAL":
        wf_id = await started_workflow(client, [task("a", requires_approval=True)])
        await make_engine(FakeLLMClient()).run_until_idle()
        return wf_id
    wf_id = await started_workflow(client, [task("a"), task("b", deps=["a"])])
    if state == "PAUSED":
        await control(client, wf_id, "pause")
    if state == "FAILED":
        await make_engine(FakeLLMClient(scripts=[FakeScript(match("a"), error=LLMPermanentError("x"))])).run_until_idle()
    return wf_id


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "PAUSED", "AWAITING_APPROVAL", "FAILED"])
async def test_cancel_from_each_allowed_state(client: AsyncClient, make_engine, state: str) -> None:  # noqa: ANN001
    wf_id = await _in_state(client, make_engine, state)
    assert (await detail(client, wf_id))["status"] == state

    resp = await control(client, wf_id, "cancel", actor="carol", reason="not needed")
    assert resp.status_code == 200, resp.text
    wf = resp.json()
    assert wf["status"] == "CANCELLED" and wf["run_requested"] is False and wf["completed_at"]
    assert wf["error"] == "cancelled by carol: not needed"
    left = {t["status"] for t in wf["tasks"]}
    assert left <= {"CANCELLED", "FAILED"}  # FAILED tasks keep their failure; nothing else survives
    payload = (await events(client, wf_id, "workflow.cancelled"))[0]["payload"]
    assert payload["actor"] == "carol" and payload["reason"] == "not needed"


async def test_cancel_discards_inflight_result_and_keeps_succeeded(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a"), task("b", deps=["a"])])
    llm = FakeLLMClient(scripts=[FakeScript(match("b"), delay=3.0)])
    engine = make_engine(llm)
    await engine.tick()
    await asyncio.gather(*engine._inflight.values())  # 'a' done
    await engine.tick()  # 'b' claimed and running
    assert (await tasks_by_key(client, wf_id))["b"]["status"] == "RUNNING"

    resp = await control(client, wf_id, "cancel")
    assert resp.status_code == 200
    assert resp.json()["error"] == "cancelled by admin: no reason given"  # defaults
    await engine.run_until_idle(timeout=2)  # local worker cancelled at once (not after 3 s)

    tasks = await tasks_by_key(client, wf_id)
    assert tasks["a"]["status"] == "SUCCEEDED" and tasks["a"]["output"]  # finished work stays visible
    assert tasks["b"]["status"] == "CANCELLED" and tasks["b"]["output"] is None
    assert outcomes(await attempts(client, wf_id, "b")) == [(1, "cancelled", None)]


async def test_cancel_cancels_pending_approvals(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await _in_state(client, make_engine, "AWAITING_APPROVAL")
    approval = await only_pending(client, wf_id)
    wf = (await control(client, wf_id, "cancel")).json()
    assert wf["pending_approvals"] == {"count": 0, "ids": []}
    row = (await client.get(f"/approvals/{approval['id']}")).json()
    assert row["status"] == "cancelled" and row["decided_by"] == "admin"
    assert (await client.post(f"/approvals/{approval['id']}/approve", json={"actor": "x"})).status_code == 409


async def test_cancel_during_planning_discards_the_plan(app: FastAPI, client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    llm = FakeLLMClient({Plan: GOOD}, delays={Plan: 0.5})
    app.dependency_overrides[get_llm_client] = lambda: llm
    wf_id = (await client.post("/workflows", json={"name": "x", "goal": "g", "run": True})).json()["id"]
    engine = make_engine(llm)
    await engine.tick()  # PLANNING, planner call in flight
    assert (await detail(client, wf_id))["status"] == "PLANNING"

    assert (await control(client, wf_id, "cancel")).status_code == 200
    await engine.run_until_idle()  # the planner returns a valid plan after the cancel

    wf = await detail(client, wf_id)
    assert wf["status"] == "CANCELLED" and wf["tasks"] == []
    assert not await events(client, wf_id, "workflow.plan_created")
    assert len(llm.calls) == 1


async def test_cancel_is_idempotent_and_409_when_completed(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a")])
    for _ in range(2):
        assert (await control(client, wf_id, "cancel")).json()["status"] == "CANCELLED"
    assert len(await events(client, wf_id, "workflow.cancelled")) == 1

    done = await started_workflow(client, [task("a")])
    await make_engine(FakeLLMClient()).run_until_idle()
    resp = await control(client, done, "cancel")
    assert resp.status_code == 409 and "COMPLETED" in resp.json()["error"]["message"]
    assert (await control(client, "00000000-0000-0000-0000-000000000000", "cancel")).status_code == 404
