from __future__ import annotations

from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import detail, event_types, started_workflow, task, tasks_by_key


async def test_linear_workflow_completes(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [
        task("a"), task("b", "analyst", ["a"]), task("c", "writer", ["b"]),
    ])
    llm = FakeLLMClient()
    await make_engine(llm).run_until_idle()

    wf = await detail(client, wf_id)
    assert wf["status"] == "COMPLETED"
    assert wf["completed_at"] is not None and wf["duration_seconds"] >= 0
    assert wf["progress"] == {"total": 3, "pending": 0, "ready": 0, "running": 0, "succeeded": 3,
                              "failed": 0, "retrying": 0, "awaiting_approval": 0, "skipped": 0,
                              "cancelled": 0}
    assert len(llm.calls) == 3

    # Result = outputs of the leaf tasks, keyed by key.
    result = (await client.get(f"/workflows/{wf_id}/result")).json()
    assert list(result["final_outputs"]) == ["c"]
    assert result["final_outputs"]["c"] == wf["tasks"][2]["output"]

    tasks = await tasks_by_key(client, wf_id)
    for t in tasks.values():
        assert t["status"] == "SUCCEEDED" and t["attempt_count"] == 1
        assert t["lease_owner"] is None and t["lease_expires_at"] is None  # lease cleared
        assert t["metrics"]["model"] == "fake-model" and t["metrics"]["output_tokens"] == 10
        assert t["started_at"] <= t["completed_at"]


async def test_events_in_order(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a"), task("b", "writer", ["a"])])
    await make_engine(FakeLLMClient()).run_until_idle()
    assert await event_types(client, wf_id) == [
        "workflow.created", "task.created", "task.created",
        "workflow.started",
        "task.ready", "task.started", "task.succeeded", "task.manifest_recorded",   # a (+ Part 12 manifest)
        "task.ready", "task.started", "task.succeeded", "task.manifest_recorded",   # b
        "workflow.completed",
    ]
    events = (await client.get(f"/workflows/{wf_id}/events")).json()
    started = next(e for e in events if e["event_type"] == "task.started")
    assert started["payload"] == {"task_key": "a", "from": "READY", "to": "RUNNING",
                                  "attempt": 1, "attempt_number": 1, "worker_id": "test-worker"}
    completed = events[-1]["payload"]
    assert completed["to"] == "COMPLETED" and completed["task_count"] == 2
    assert completed["output_tokens"] == 20 and completed["duration_seconds"] >= 0


async def test_task_detail_endpoint(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a")])
    await make_engine(FakeLLMClient()).run_until_idle()
    resp = await client.get(f"/workflows/{wf_id}/tasks/a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"]["summary"] and body["metrics"]["latency_ms"] == 1
    assert body["error"] is None and body["error_type"] is None
    assert (await client.get(f"/workflows/{wf_id}/tasks/nope")).status_code == 404


async def test_graph_reflects_live_status(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a"), task("b", "writer", ["a"])])
    before = (await client.get(f"/workflows/{wf_id}/graph")).json()
    assert {n["status"] for n in before["nodes"]} == {"PENDING"}
    await make_engine(FakeLLMClient()).run_until_idle()
    after = (await client.get(f"/workflows/{wf_id}/graph")).json()
    assert {n["status"] for n in after["nodes"]} == {"SUCCEEDED"}
