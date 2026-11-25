from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.enums import TaskStatus
from app.engine.scheduler import claim_tasks, schedule_workflow
from app.engine.worker import execute_task
from app.llm.fake_client import FakeLLMClient
from app.models import Task
from tests.engine_helpers import started_workflow, task, tasks_by_key


@pytest.mark.parametrize(
    "change",
    [{"lease_owner": "someone-else"}, {"status": TaskStatus.CANCELLED, "lease_owner": None}],
    ids=["lease-taken-over", "status-changed"],
)
async def test_worker_does_not_overwrite_changed_task(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], change: dict
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    async with session_factory() as s:
        await schedule_workflow(s, uuid.UUID(wf_id))
    async with session_factory() as s:
        (claim,) = await claim_tasks(s, "w1", limit=1, lease_seconds=60)
    task_id = claim.task_id

    # Someone else (Part 5 recovery / Part 6 cancel) changes the task while w1 is working.
    async with session_factory() as s:
        await s.execute(update(Task).where(Task.id == task_id).values(**change))
        await s.commit()

    saved = await execute_task(claim, session_factory, FakeLLMClient(), get_settings(), "w1")
    assert saved is False

    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["output"] is None and t["metrics"] is None
    assert t["status"] == change.get("status", "RUNNING")
    assert t["lease_owner"] == change["lease_owner"]
