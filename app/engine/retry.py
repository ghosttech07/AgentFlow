"""Retry policy and attempt bookkeeping. Decisions come from error_type + DB state, never the LLM.

| error_type      | retryable | source                                          |
|-----------------|-----------|-------------------------------------------------|
| transient       | yes       | timeouts inside the SDK, per-minute 429, 5xx    |
| overloaded      | yes       | 503 high demand (after the model fallback chain) |
| timeout         | yes       | TASK_TIMEOUT_SECONDS expired                    |
| output          | yes       | invalid JSON after the in-client repair         |
| unknown         | yes       | unexpected exception (bug or infra hiccup)      |
| worker_lost     | yes       | lease expired / process died mid-task           |
| permanent       | no        | 400/401/403/404, safety block                   |
| quota_exhausted | no        | daily / plan quota: waiting minutes won't help  |

delay(attempt) = min(MAX, BASE * 2**(attempt-1)); with RETRY_JITTER, full jitter: uniform(0, delay);
if the error carries retry_after: max(retry_after, delay), always capped at RETRY_MAX_DELAY_SECONDS.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.enums import TaskStatus
from app.core.events import EventType, record_event
from app.core.state_machine import transition_task
from app.db.base import utcnow
from app.models.attempt import TaskAttempt
from app.models.task import Task

RETRYABLE = frozenset({"transient", "overloaded", "timeout", "output", "unknown", "worker_lost"})
NOT_RETRYABLE = frozenset({"permanent", "quota_exhausted"})


def compute_delay(
    attempt: int,
    settings: Settings,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> float:
    delay = min(settings.retry_max_delay_seconds, settings.retry_base_delay_seconds * 2 ** max(attempt - 1, 0))
    if settings.retry_jitter:
        delay = rand() * delay  # full jitter spreads retries of many tasks apart
    if retry_after is not None:
        delay = max(retry_after, delay)
    return min(delay, settings.retry_max_delay_seconds)


async def fail_or_retry(
    session: AsyncSession,
    task: Task,
    settings: Settings,
    *,
    error_type: str,
    error: str,
    retry_after: float | None = None,
) -> TaskStatus:
    """Apply the retry policy to a locked RUNNING task: RETRYING (with backoff) or FAILED.

    Clears the lease. The workflow stays RUNNING on RETRYING; the scheduler fails it on FAILED.
    """
    task.lease_owner = None
    task.lease_expires_at = None
    task.error_type = error_type
    task.error = error
    if error_type in RETRYABLE and task.attempt_count < task.max_attempts:
        delay = compute_delay(task.attempt_count, settings, retry_after)
        task.next_attempt_at = utcnow() + timedelta(seconds=delay)
        await transition_task(
            session, task, TaskStatus.RETRYING, event_type=EventType.TASK_RETRY_SCHEDULED,
            extra={"attempt": task.attempt_count, "max_attempts": task.max_attempts,
                   "error_type": error_type, "delay": round(delay, 3), "error": error[:500]},
        )
        return TaskStatus.RETRYING
    reason = "not retryable" if error_type not in RETRYABLE else "max attempts reached"
    task.next_attempt_at = None
    await transition_task(
        session, task, TaskStatus.FAILED, event_type=EventType.TASK_FAILED,
        extra={"attempt": task.attempt_count, "error_type": error_type, "error": error[:500],
               "reason": reason},
    )
    return TaskStatus.FAILED


async def open_attempt(session: AsyncSession, task: Task, worker_id: str) -> TaskAttempt:
    """Insert the attempt row at claim time (same transaction as the claim)."""
    last = await session.scalar(
        select(func.max(TaskAttempt.attempt_number)).where(TaskAttempt.task_id == task.id)
    )
    attempt = TaskAttempt(
        task_id=task.id, workflow_id=task.workflow_id, attempt_number=(last or 0) + 1,
        worker_id=worker_id, started_at=utcnow(), outcome="running",
    )
    session.add(attempt)
    return attempt


async def close_attempt(
    session: AsyncSession,
    task_id: object,
    *,
    outcome: str,
    error_type: str | None = None,
    error: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    provider: str | None = None,
    cost_usd: float | None = None,
) -> None:
    """Finish the task's open (outcome=running) attempt row, if any."""
    attempt = await session.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task_id, TaskAttempt.outcome == "running")
        .order_by(TaskAttempt.attempt_number.desc())
        .limit(1)
    )
    if attempt is None:
        return
    attempt.outcome = outcome
    attempt.finished_at = utcnow()
    attempt.error_type = error_type
    attempt.error = error[:2000] if error else None
    attempt.model = model
    attempt.input_tokens = input_tokens
    attempt.output_tokens = output_tokens
    attempt.latency_ms = latency_ms
    attempt.provider = provider
    attempt.cost_usd = cost_usd


async def reclaim_lost_task(
    session: AsyncSession, task: Task, settings: Settings, *, reason: str
) -> TaskStatus:
    """A RUNNING task whose worker is gone (lease expired or process restarted): record
    worker_lost on its attempt and apply the retry policy. Task must be locked by the caller."""
    previous_owner = task.lease_owner
    message = f"Worker {previous_owner or '?'} lost ({reason}) during attempt {task.attempt_count}"
    await close_attempt(session, task.id, outcome="worker_lost", error_type="worker_lost", error=message)
    record_event(
        session, workflow_id=task.workflow_id, task_id=task.id,
        event_type=EventType.TASK_LEASE_EXPIRED,
        payload={"task_key": task.key, "previous_owner": previous_owner,
                 "attempt": task.attempt_count, "reason": reason},
    )
    return await fail_or_retry(session, task, settings, error_type="worker_lost", error=message)
