from __future__ import annotations

import asyncio

from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.engine_helpers import match, started_workflow, task, tasks_by_key


async def test_shutdown_cancels_after_grace_and_leaves_task_running(
    client: AsyncClient, make_engine  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(client, [task("quick"), task("stuck")])
    llm = FakeLLMClient(scripts=[FakeScript(match("stuck"), hang=True)])
    engine = make_engine(llm, engine_shutdown_grace_seconds=0.5, engine_poll_interval_seconds=0.05)

    await engine.start()
    for _ in range(100):  # wait until 'quick' is done and 'stuck' is in flight
        tasks = await tasks_by_key(client, wf_id)
        if tasks["quick"]["status"] == "SUCCEEDED" and tasks["stuck"]["status"] == "RUNNING":
            break
        await asyncio.sleep(0.05)
    assert len(engine.status()["in_flight_task_ids"]) == 1

    await engine.stop()

    tasks = await tasks_by_key(client, wf_id)
    assert tasks["quick"]["status"] == "SUCCEEDED"
    # Cancelled in-flight work is NOT marked failed: it stays RUNNING with its lease for Part 5.
    assert tasks["stuck"]["status"] == "RUNNING"
    assert tasks["stuck"]["lease_owner"] == "test-worker" and tasks["stuck"]["lease_expires_at"]
    assert engine.status()["running"] is False and engine.status()["in_flight_task_ids"] == []


async def test_shutdown_waits_for_tasks_that_finish_within_grace(
    client: AsyncClient, make_engine  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    engine = make_engine(FakeLLMClient(delay=0.3), engine_shutdown_grace_seconds=5,
                         engine_poll_interval_seconds=0.05)
    await engine.start()
    while not engine.status()["in_flight_task_ids"]:
        await asyncio.sleep(0.02)
    await engine.stop()  # drains: the 0.3 s task finishes inside the grace period
    assert (await tasks_by_key(client, wf_id))["a"]["status"] == "SUCCEEDED"
