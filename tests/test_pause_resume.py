from __future__ import annotations

import asyncio

from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.control_helpers import control, events
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, calls_for, outcomes


async def test_graceful_pause_finishes_inflight_and_claims_nothing_new(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    # One slot: 'a' runs, 'c' is READY but unclaimed, 'b' waits for 'a'.
    wf_id = await started_workflow(client, [task("a"), task("c"), task("b", deps=["a"])])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), delay=0.3)])
    engine = make_engine(llm, engine_max_concurrency=1)
    await engine.tick()  # claims 'a' only

    resp = await control(client, wf_id, "pause", actor="alice", reason="lunch")
    assert resp.status_code == 200 and resp.json()["status"] == "PAUSED"
    await engine.run_until_idle()

    tasks = await tasks_by_key(client, wf_id)
    assert tasks["a"]["status"] == "SUCCEEDED" and tasks["a"]["output"]  # in-flight work was saved
    assert tasks["c"]["status"] == "READY" and tasks["c"]["attempt_count"] == 0  # never claimed
    assert tasks["b"]["status"] == "PENDING"  # never promoted
    assert (await detail(client, wf_id))["status"] == "PAUSED"
    paused = (await events(client, wf_id, "workflow.paused"))[0]["payload"]
    assert paused["actor"] == "alice" and paused["reason"] == "lunch" and paused["mode"] == "graceful"

    resp = await control(client, wf_id, "resume", actor="alice")
    assert resp.status_code == 200 and resp.json()["status"] == "RUNNING"
    assert (await events(client, wf_id, "workflow.resumed"))[0]["payload"]["actor"] == "alice"
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert [calls_for(llm, k) for k in "acb"] == [1, 1, 1]  # succeeded work never re-run


async def test_immediate_pause_interrupts_without_using_a_retry(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a")])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), delay=5.0, times=1)])  # 2nd call is instant
    engine = make_engine(llm)
    await engine.tick()
    worker = next(iter(engine._inflight.values()))

    resp = await control(client, wf_id, "pause", mode="immediate", actor="ops")
    assert resp.status_code == 200
    await asyncio.wait({worker}, timeout=2)
    assert worker.cancelled()  # the local worker was cancelled right away
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "READY" and t["lease_owner"] is None and t["output"] is None
    # attempt_count is the fencing token (never decremented); the budget is extended instead.
    assert (t["attempt_count"], t["max_attempts"]) == (1, 4)
    assert outcomes(await attempts(client, wf_id, "a")) == [(1, "interrupted", None)]
    assert (await events(client, wf_id, "task.interrupted"))[0]["payload"]["actor"] == "ops"

    await engine.run_until_idle()
    assert (await tasks_by_key(client, wf_id))["a"]["status"] == "READY"  # paused: not re-claimed
    await control(client, wf_id, "resume")
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert outcomes(await attempts(client, wf_id, "a")) == [(1, "interrupted", None), (2, "succeeded", None)]


async def test_remote_worker_detects_immediate_pause_via_heartbeat(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a")])
    remote = make_engine(FakeLLMClient(scripts=[FakeScript(match("a"), delay=3.0)]),
                         engine_worker_id="remote", heartbeat_interval_seconds=0.05)
    await remote.tick()
    worker = next(iter(remote._inflight.values()))
    make_engine(FakeLLMClient())  # the API now talks to another engine: no local cancel possible

    await control(client, wf_id, "pause", mode="immediate")
    done, _ = await asyncio.wait({worker}, timeout=2)
    assert done  # stopped long before its 3 s call ended: the heartbeat saw the status change
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "READY" and t["output"] is None


async def test_pause_resume_idempotency_and_conflicts(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    pending = (await client.post("/workflows", json={"name": "x", "goal": "g", "tasks": [task("a")]})).json()["id"]
    assert (await control(client, pending, "pause")).status_code == 409
    assert (await control(client, pending, "resume")).status_code == 409

    wf_id = await started_workflow(client, [task("a")])
    for _ in range(2):  # double click
        resp = await control(client, wf_id, "pause")
        assert resp.status_code == 200 and resp.json()["status"] == "PAUSED"
    assert len(await events(client, wf_id, "workflow.paused")) == 1
    assert (await control(client, wf_id, "resume")).status_code == 200
    assert (await control(client, wf_id, "resume")).status_code == 409  # already RUNNING

    await make_engine(FakeLLMClient()).run_until_idle()
    assert (await control(client, wf_id, "pause")).status_code == 409  # COMPLETED
    bad = await client.post(f"/workflows/{wf_id}/pause", json={"mode": "sideways"})
    assert bad.status_code == 422


async def test_paused_workflow_stays_paused_after_restart(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a"), task("b", deps=["a"])])
    first = make_engine(FakeLLMClient(delay=5.0))
    await first.tick()  # 'a' RUNNING
    await control(client, wf_id, "pause")  # graceful
    for w in first._inflight.values():  # simulated crash: the worker dies, 'a' stays RUNNING
        w.cancel()
    await asyncio.gather(*first._inflight.values(), return_exceptions=True)

    llm = FakeLLMClient()
    restarted = make_engine(llm, engine_worker_id="after-restart")
    await restarted.recover()
    await restarted.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "PAUSED"
    assert (await tasks_by_key(client, wf_id))["a"]["status"] == "RETRYING"  # reclaimed, not promoted
    assert llm.calls == []

    await control(client, wf_id, "resume")
    await restarted.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert outcomes(await attempts(client, wf_id, "a")) == [(1, "worker_lost", "worker_lost"), (2, "succeeded", None)]
