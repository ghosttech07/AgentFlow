from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.base import utcnow
from app.engine.retry import compute_delay
from app.engine.scheduler import next_retry_due
from app.llm.errors import LLMPermanentError, LLMQuotaExhaustedError, LLMTransientError
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models import Task
from tests.engine_helpers import detail, event_types, match, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, calls_for, outcomes

S = get_settings().model_copy(update={"retry_base_delay_seconds": 2, "retry_max_delay_seconds": 60,
                                      "retry_jitter": True})


# ---------------------------------------------------------------- backoff math


@pytest.mark.parametrize(("attempt", "ceiling"), [(1, 2), (2, 4), (3, 8), (6, 60), (20, 60)])
def test_backoff_with_full_jitter_bounds(attempt: int, ceiling: float) -> None:
    assert compute_delay(attempt, S, rand=lambda: 1.0) == ceiling
    assert compute_delay(attempt, S, rand=lambda: 0.0) == 0
    for _ in range(200):
        assert 0 <= compute_delay(attempt, S) <= ceiling


def test_backoff_without_jitter() -> None:
    s = S.model_copy(update={"retry_jitter": False})
    assert [compute_delay(n, s) for n in (1, 2, 3, 4)] == [2, 4, 8, 16]


def test_retry_after_respected_and_capped() -> None:
    assert compute_delay(1, S, retry_after=30, rand=lambda: 0.0) == 30
    assert compute_delay(1, S, retry_after=0.5, rand=lambda: 1.0) == 2  # computed is larger
    assert compute_delay(1, S, retry_after=59459, rand=lambda: 0.5) == 60  # capped


# ---------------------------------------------------------------- engine behaviour


async def test_transient_then_success(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a")])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), error=LLMTransientError("503-ish"), times=1)])
    await make_engine(llm).run_until_idle()

    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "SUCCEEDED" and t["attempt_count"] == 2 and t["error"] is None
    assert outcomes(await attempts(client, wf_id, "a")) == [(1, "failed", "transient"), (2, "succeeded", None)]
    types = await event_types(client, wf_id)
    assert types.count("task.retry_scheduled") == 1 and types.count("task.started") == 2
    assert "workflow.failed" not in types


@pytest.mark.parametrize(
    ("error", "error_type"),
    [(LLMPermanentError("403"), "permanent"), (LLMQuotaExhaustedError("daily quota"), "quota_exhausted")],
)
async def test_non_retryable_fails_immediately(
    client: AsyncClient, make_engine, error: Exception, error_type: str  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), error=error)])
    await make_engine(llm).run_until_idle()
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "FAILED" and t["error_type"] == error_type and t["attempt_count"] == 1
    assert calls_for(llm, "a") == 1
    assert (await detail(client, wf_id))["status"] == "FAILED"
    assert "task.retry_scheduled" not in await event_types(client, wf_id)


async def test_retries_exhausted(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a"), task("b", "writer", ["a"])])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), error=LLMTransientError("still down"))])
    await make_engine(llm).run_until_idle()

    tasks = await tasks_by_key(client, wf_id)
    assert tasks["a"]["status"] == "FAILED" and tasks["a"]["attempt_count"] == 3
    assert tasks["b"]["status"] == "PENDING"
    assert calls_for(llm, "a") == 3
    assert [o[1] for o in outcomes(await attempts(client, wf_id, "a"))] == ["failed"] * 3
    wf = await detail(client, wf_id)
    assert wf["status"] == "FAILED" and "Task 'a' failed (transient)" in wf["error"]


async def test_retrying_task_not_claimed_before_due(
    client: AsyncClient, make_engine, session_factory: async_sessionmaker[AsyncSession]  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), error=LLMTransientError("x"), times=1)])
    engine = make_engine(llm, retry_base_delay_seconds=30, retry_max_delay_seconds=60, retry_jitter=False)

    await engine.tick()  # promote + claim attempt 1
    await asyncio.gather(*list(engine._inflight.values()))
    for _ in range(3):
        await engine.tick()
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "RETRYING" and calls_for(llm, "a") == 1  # not claimed yet
    due = await next_retry_due(session_factory)
    assert due is not None and 25 < (due - utcnow()).total_seconds() <= 30  # 5 s slack: ticks are slower under load

    # The engine loop sleeps until the retry is due (not just the poll interval).
    engine.settings = engine.settings.model_copy(update={"engine_poll_interval_seconds": 120})
    assert 25 < await engine._sleep_seconds() <= 30

    async with session_factory() as s:  # time passes
        await s.execute(update(Task).where(Task.id == uuid.UUID(t["id"]))
                        .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        await s.commit()
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert calls_for(llm, "a") == 2
