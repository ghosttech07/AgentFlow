"""Simulate a crash directly in the DB, then recover + run with a fresh engine."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.planner import Plan
from app.core.enums import TaskStatus, WorkflowStatus
from app.db.base import utcnow
from app.llm.fake_client import FakeLLMClient
from app.models import Task, TaskAttempt, Workflow
from tests.engine_helpers import detail, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, calls_for, outcomes
from tests.test_planner import GOOD


async def crashed_state(client: AsyncClient, sf: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    """wf1: a SUCCEEDED, b RUNNING (owner died, lease still valid), c PENDING.
    wf2: stuck in PLANNING with no tasks."""
    wf1 = await started_workflow(client, [task("a"), task("b", deps=["a"]), task("c", "writer", ["b"])])
    ids = {k: uuid.UUID(t["id"]) for k, t in (await tasks_by_key(client, wf1)).items()}
    wf2 = (await client.post("/workflows", json={"name": "p", "goal": "Compare EVs"})).json()["id"]
    now = utcnow()
    async with sf() as s:
        await s.execute(update(Task).where(Task.id == ids["a"]).values(
            status=TaskStatus.SUCCEEDED, attempt_count=1, output={"summary": "A-DONE"},
            started_at=now, completed_at=now))
        await s.execute(update(Task).where(Task.id == ids["b"]).values(
            status=TaskStatus.RUNNING, attempt_count=1, lease_owner="dead-process",
            lease_expires_at=now + timedelta(seconds=300), started_at=now))
        s.add_all([
            TaskAttempt(task_id=ids["a"], workflow_id=uuid.UUID(wf1), attempt_number=1,
                        worker_id="dead-process", outcome="succeeded", finished_at=now),
            TaskAttempt(task_id=ids["b"], workflow_id=uuid.UUID(wf1), attempt_number=1,
                        worker_id="dead-process", outcome="running"),
        ])
        await s.execute(update(Workflow).where(Workflow.id == uuid.UUID(wf2))
                        .values(status=WorkflowStatus.PLANNING))
        await s.commit()
    return wf1, wf2


async def test_restart_preserves_completed_work(
    app: FastAPI, client: AsyncClient, make_engine,  # noqa: ANN001
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    wf1, wf2 = await crashed_state(client, session_factory)
    llm = FakeLLMClient({Plan: GOOD})
    engine = make_engine(llm, engine_single_node=True)

    counts = await engine.recover()
    assert counts["reclaimed_tasks"] == 1 and counts["replanned_workflows"] == 1
    await engine.run_until_idle()

    assert (await detail(client, wf1))["status"] == "COMPLETED"
    assert (await detail(client, wf2))["status"] == "COMPLETED"  # re-planned, then run

    # The agent of the task that SUCCEEDED before the crash was never called again.
    assert calls_for(llm, "a") == 0
    assert calls_for(llm, "b") == 1 and calls_for(llm, "c") == 1
    assert (await tasks_by_key(client, wf1))["a"]["output"] == {"summary": "A-DONE"}

    assert outcomes(await attempts(client, wf1, "a")) == [(1, "succeeded", None)]
    assert outcomes(await attempts(client, wf1, "b")) == [(1, "worker_lost", "worker_lost"),
                                                         (2, "succeeded", None)]
    assert outcomes(await attempts(client, wf1, "c")) == [(1, "succeeded", None)]

    events1 = (await client.get(f"/workflows/{wf1}/events")).json()
    recovered = next(e for e in events1 if e["event_type"] == "engine.recovered")
    assert recovered["payload"]["reclaimed_task_keys"] == ["b"]
    types2 = [e["event_type"] for e in (await client.get(f"/workflows/{wf2}/events")).json()]
    assert "workflow.planning_recovered" in types2 and "engine.recovered" in types2


async def test_multi_node_mode_leaves_live_leases_alone(
    client: AsyncClient, make_engine, session_factory: async_sessionmaker[AsyncSession]  # noqa: ANN001
) -> None:
    wf1, _ = await crashed_state(client, session_factory)
    engine = make_engine(FakeLLMClient(), engine_single_node=False)
    counts = await engine.recover()
    assert counts["reclaimed_tasks"] == 0
    b = (await tasks_by_key(client, wf1))["b"]
    assert b["status"] == "RUNNING" and b["lease_owner"] == "dead-process"  # reaper waits for expiry
