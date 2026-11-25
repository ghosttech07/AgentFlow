from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from app.agents.planner import Plan
from app.llm import get_llm_client
from app.llm.errors import LLMPermanentError
from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, calls_for, outcomes
from tests.test_planner import CYCLIC, GOOD

GRAPH = [task("a"), task("b", deps=["a"]), task("c", deps=["a"]), task("d", "writer", ["b", "c"])]


async def test_retry_reruns_only_failed_tasks(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GRAPH)
    llm = FakeLLMClient(scripts=[FakeScript(match("b"), error=LLMPermanentError("bad request"), times=1)])
    engine = make_engine(llm)
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "FAILED"

    resp = await client.post(f"/workflows/{wf_id}/retry")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "RUNNING" and body["error"] is None
    by_key = {t["key"]: t for t in body["tasks"]}
    assert by_key["b"]["status"] == "PENDING" and by_key["b"]["attempt_count"] == 0
    assert by_key["a"]["status"] == "SUCCEEDED" and by_key["c"]["status"] == "SUCCEEDED"

    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert (calls_for(llm, "a"), calls_for(llm, "c")) == (1, 1)  # succeeded work not redone
    assert calls_for(llm, "b") == 2 and calls_for(llm, "d") == 1
    # Attempt numbers continue after the old ones although attempt_count was reset.
    assert outcomes(await attempts(client, wf_id, "b")) == [(1, "failed", "permanent"), (2, "succeeded", None)]
    assert (await tasks_by_key(client, wf_id))["b"]["attempt_count"] == 1
    events = (await client.get(f"/workflows/{wf_id}/events")).json()
    retried = next(e for e in events if e["event_type"] == "workflow.retried")
    assert retried["payload"]["reset_tasks"] == ["b"]


async def test_retry_409_unless_failed(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    pending = (await client.post("/workflows", json={"name": "x", "goal": "g"})).json()["id"]
    assert (await client.post(f"/workflows/{pending}/retry")).status_code == 409
    done = await started_workflow(client, [task("a")])
    await make_engine(FakeLLMClient()).run_until_idle()
    resp = await client.post(f"/workflows/{done}/retry")
    assert resp.status_code == 409 and "Only FAILED" in resp.json()["error"]["message"]
    assert (await client.post("/workflows/00000000-0000-0000-0000-000000000000/retry")).status_code == 404


async def test_retry_after_planning_failure_replans(app: FastAPI, client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    bad = FakeLLMClient({Plan: [CYCLIC] * 3})
    app.dependency_overrides[get_llm_client] = lambda: bad
    wf_id = (await client.post("/workflows", json={"name": "x", "goal": "g", "run": True})).json()["id"]
    await make_engine(bad).run_until_idle()
    assert (await detail(client, wf_id))["status"] == "FAILED"

    resp = await client.post(f"/workflows/{wf_id}/retry")
    assert resp.status_code == 202 and resp.json()["run_requested"] is True
    await make_engine(FakeLLMClient({Plan: GOOD})).run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "COMPLETED" and len(wf["tasks"]) == 5
