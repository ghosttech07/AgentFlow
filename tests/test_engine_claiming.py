from __future__ import annotations

import asyncio
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.engine.scheduler import claim_tasks, schedule_workflow
from tests.engine_helpers import started_workflow, task, tasks_by_key


async def test_concurrent_claims_never_overlap(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wf_id = await started_workflow(client, [task(f"t{i}") for i in range(8)])
    async with session_factory() as s:
        assert await schedule_workflow(s, uuid.UUID(wf_id)) == 8  # all promoted to READY

    async def claim(worker: str) -> list[uuid.UUID]:
        async with session_factory() as s:
            return [c.task_id for c in await claim_tasks(s, worker, limit=8, lease_seconds=60)]

    first, second = await asyncio.gather(claim("w1"), claim("w2"))
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 8

    tasks = await tasks_by_key(client, wf_id)
    owners = {t["lease_owner"] for t in tasks.values()}
    assert owners <= {"w1", "w2"}
    for t in tasks.values():
        assert t["status"] == "RUNNING" and t["attempt_count"] == 1 and t["lease_expires_at"]

    # Nothing left to claim.
    async with session_factory() as s:
        assert await claim_tasks(s, "w3", limit=8, lease_seconds=60) == []
