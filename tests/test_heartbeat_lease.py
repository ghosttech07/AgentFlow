from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import AgentResult
from app.config import get_settings
from app.db.base import utcnow
from app.engine.scheduler import Claim, claim_tasks, reap_expired_leases, schedule_workflow
from app.engine.worker import _save, execute_task
from app.llm.fake_client import FakeLLMClient
from app.models import Task
from tests.engine_helpers import event_types, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, outcomes

FAST = {"heartbeat_interval_seconds": 0.1, "lease_duration_seconds": 0.5,
        "retry_base_delay_seconds": 0.01, "retry_max_delay_seconds": 0.05}


async def claim_one(sf: async_sessionmaker[AsyncSession], wf_id: str, worker: str, lease: float) -> Claim:
    async with sf() as s:
        await schedule_workflow(s, uuid.UUID(wf_id))
    async with sf() as s:
        (claim,) = await claim_tasks(s, worker, limit=1, lease_seconds=lease)
    return claim


async def test_heartbeats_keep_a_long_task_alive(
    client: AsyncClient, make_engine, session_factory: async_sessionmaker[AsyncSession]  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(client, [task("long")])
    engine = make_engine(FakeLLMClient(delay=1.5), **FAST)  # task 3x longer than the lease

    stop = asyncio.Event()

    async def aggressive_reaper() -> int:
        reaped = 0
        while not stop.is_set():
            reaped += await reap_expired_leases(session_factory, engine.settings)
            await asyncio.sleep(0.05)
        return reaped

    reaper = asyncio.create_task(aggressive_reaper())
    await engine.run_until_idle()
    stop.set()
    assert await reaper == 0  # never expired: heartbeats extended the lease

    t = (await tasks_by_key(client, wf_id))["long"]
    assert t["status"] == "SUCCEEDED" and t["attempt_count"] == 1
    assert t["last_heartbeat_at"] is not None


async def test_lost_lease_cancels_worker_and_discards_result(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    claim = await claim_one(session_factory, wf_id, "w1", lease=60)
    settings = get_settings().model_copy(update=FAST)

    async def steal() -> None:
        await asyncio.sleep(0.15)
        async with session_factory() as s:
            await s.execute(update(Task).where(Task.id == claim.task_id).values(lease_owner="thief"))
            await s.commit()

    t0 = time.perf_counter()
    saved, _ = await asyncio.gather(
        execute_task(claim, session_factory, FakeLLMClient(delay=5), settings, "w1"), steal()
    )
    assert saved is False
    assert time.perf_counter() - t0 < 2  # cancelled at the next heartbeat, not after 5 s
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["lease_owner"] == "thief" and t["status"] == "RUNNING" and t["output"] is None


async def test_reaper_turns_expired_lease_into_worker_lost(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    await claim_one(session_factory, wf_id, "dead-worker", lease=-1)  # already expired

    settings = get_settings().model_copy(update=FAST)
    assert await reap_expired_leases(session_factory, settings) == 1

    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "RETRYING" and t["error_type"] == "worker_lost"
    assert t["lease_owner"] is None and t["next_attempt_at"] is not None
    assert outcomes(await attempts(client, wf_id, "a")) == [(1, "worker_lost", "worker_lost")]
    events = (await client.get(f"/workflows/{wf_id}/events")).json()
    lost = next(e for e in events if e["event_type"] == "task.lease_expired")
    assert lost["payload"]["previous_owner"] == "dead-worker" and lost["payload"]["attempt"] == 1
    assert "task.retry_scheduled" in await event_types(client, wf_id)


async def test_zombie_worker_cannot_overwrite_newer_attempt(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    settings = get_settings().model_copy(update=FAST)
    old = await claim_one(session_factory, wf_id, "w", lease=-1)  # attempt 1, then "hangs"
    await reap_expired_leases(session_factory, settings)
    async with session_factory() as s:  # backoff elapsed
        await s.execute(update(Task).where(Task.id == old.task_id)
                        .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    new = await claim_one(session_factory, wf_id, "w", lease=60)  # attempt 2, SAME worker id
    assert (old.attempt, new.attempt) == (1, 2)

    zombie_result = AgentResult(agent_type="researcher", output={"stale": True}, model="m",
                                input_tokens=1, output_tokens=1, latency_ms=1)
    assert await _save(session_factory, old, "w", settings, result=zombie_result) is False

    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "RUNNING" and t["attempt_count"] == 2 and t["output"] is None
    assert outcomes(await attempts(client, wf_id, "a")) == [(1, "worker_lost", "worker_lost"),
                                                           (2, "running", None)]
