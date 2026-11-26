"""Execute one claimed task with its agent.

Rules:
- Own DB session per step; no transaction is open during the LLM call.
- While the agent runs, heartbeat every HEARTBEAT_INTERVAL_SECONDS with a conditional UPDATE
  (status RUNNING, lease_owner = me, attempt_count = my attempt). 0 rows -> ownership lost:
  cancel the agent call and discard its result.
- Idempotency guard: every save is conditional on the same three columns, so a slow "zombie"
  worker can never overwrite a newer attempt (Part 5) or a cancelled task (Part 6).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import AgentInput, AgentResult, create_agent
from app.config import Settings
from app.core.enums import TaskStatus
from app.core.events import EventType
from app.core.state_machine import transition_task
from app.db.base import utcnow
from app.engine import chaos
from app.engine.context import build_agent_input
from app.engine.retry import close_attempt, fail_or_retry
from app.engine.scheduler import Claim, task_metrics
from app.llm.base import LLMClient
from app.llm.context import CallContext, use_context
from app.llm.errors import BudgetPause, LLMError
from app.models.task import Task

logger = logging.getLogger(__name__)


def classify_failure(exc: BaseException) -> str:
    """error_type stored on the task; the retry policy (app.engine.retry) decides from it."""
    if isinstance(exc, TimeoutError):  # asyncio.wait_for(TASK_TIMEOUT_SECONDS) expired
        return "timeout"
    if isinstance(exc, LLMError):
        return exc.error_type  # transient | overloaded | quota_exhausted | permanent | output
    return "unknown"


async def execute_task(
    claim: Claim | uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    settings: Settings,
    worker_id: str,
) -> bool:
    """Run the task's agent and save the outcome. Returns True if the outcome was saved."""
    if not isinstance(claim, Claim):  # convenience for callers that only know the id
        async with session_factory() as session:
            task = await session.get(Task, claim)
            claim = Claim(claim, task.attempt_count if task else 0)

    async with session_factory() as session:
        task, agent_input = await build_agent_input(session, claim.task_id)
        agent_type, key, workflow_id = task.agent_type, task.key, task.workflow_id
        has_chaos = await chaos.has_rule(session, workflow_id, key, settings)
    # The session is closed here: nothing is locked while the model thinks.

    logger.info("task.running key=%s agent=%s attempt=%d", key, agent_type, claim.attempt)
    with use_context(CallContext(workflow_id, claim.task_id)):  # inherited by the agent's asyncio task
        agent_call = asyncio.create_task(
            _run_agent(agent_type, agent_input, llm, session_factory, workflow_id, key, settings, has_chaos),
            name=f"agent-{key}",
        )
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat(session_factory, claim, worker_id, settings, lease_lost, agent_call),
        name=f"heartbeat-{key}",
    )
    try:
        result = await agent_call
    except asyncio.CancelledError:
        if lease_lost.is_set() and not _current_task_cancelling():
            logger.warning("task.result_discarded key=%s reason=lease_lost attempt=%d", key, claim.attempt)
            return False
        agent_call.cancel()
        raise  # shutdown: leave the task RUNNING with its lease (recovery reclaims it)
    except BudgetPause as pause:  # the router paused the workflow before calling: not a failure
        return await _budget_return(session_factory, claim, worker_id, pause.reason)
    except Exception as exc:
        error_type = classify_failure(exc)
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        if error_type == "timeout":
            message = f"Task timed out after {settings.task_timeout_seconds:g}s"
        if error_type == "unknown":
            logger.exception("task.unexpected_error key=%s task=%s", key, claim.task_id)
        return await _save(session_factory, claim, worker_id, settings,
                           error=(error_type, message, getattr(exc, "retry_after_seconds", None)))
    finally:
        heartbeat.cancel()
    return await _save(session_factory, claim, worker_id, settings, result=result)


def _current_task_cancelling() -> bool:
    task = asyncio.current_task()
    return bool(task and task.cancelling())


async def _run_agent(
    agent_type: str,
    agent_input: AgentInput,
    llm: LLMClient,
    session_factory: async_sessionmaker[AsyncSession],
    workflow_id: uuid.UUID,
    key: str,
    settings: Settings,
    has_chaos: bool,
) -> AgentResult:
    async def run() -> AgentResult:
        if has_chaos:  # dev fault injection
            await chaos.inject(session_factory, workflow_id, key, settings)
        agent = create_agent(agent_type, llm)
        agent.session_factory = session_factory  # same DB as the engine (publisher side effects)
        return await agent.run(agent_input)

    return await asyncio.wait_for(run(), timeout=settings.task_timeout_seconds)


async def _heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    claim: Claim,
    worker_id: str,
    settings: Settings,
    lease_lost: asyncio.Event,
    agent_call: asyncio.Task[AgentResult],
) -> None:
    """Extend the lease while the agent runs; cancel the agent if ownership is lost."""
    while True:
        await asyncio.sleep(settings.heartbeat_interval_seconds)
        try:
            async with session_factory() as session:
                now = utcnow()
                res = await session.execute(
                    update(Task)
                    .where(Task.id == claim.task_id, Task.status == TaskStatus.RUNNING,
                           Task.lease_owner == worker_id, Task.attempt_count == claim.attempt)
                    .values(last_heartbeat_at=now,
                            lease_expires_at=now + timedelta(seconds=settings.lease_duration_seconds))
                )
                await session.commit()
        except Exception:
            logger.exception("task.heartbeat_failed task=%s (will retry)", claim.task_id)
            continue
        if res.rowcount == 0:
            logger.warning("task.lease_lost task=%s attempt=%d worker=%s - cancelling agent call",
                           claim.task_id, claim.attempt, worker_id)
            lease_lost.set()
            agent_call.cancel()
            return


async def _save(
    session_factory: async_sessionmaker[AsyncSession],
    claim: Claim,
    worker_id: str,
    settings: Settings,
    *,
    result: AgentResult | None = None,
    error: tuple[str, str, float | None] | None = None,
) -> bool:
    async with session_factory() as session:
        task = await session.scalar(select(Task).where(Task.id == claim.task_id).with_for_update())
        if (task is None or task.status != TaskStatus.RUNNING or task.lease_owner != worker_id
                or task.attempt_count != claim.attempt):
            logger.warning(
                "task.result_discarded task=%s status=%s lease_owner=%s attempt=%s worker=%s my_attempt=%d",
                claim.task_id, getattr(task, "status", None), getattr(task, "lease_owner", None),
                getattr(task, "attempt_count", None), worker_id, claim.attempt,
            )
            await session.rollback()
            return False
        if result is not None:
            task.lease_owner = None
            task.lease_expires_at = None
            task.output = result.output
            task.metrics = task_metrics(result)
            task.error = task.error_type = None
            await close_attempt(session, task.id, outcome="succeeded", model=result.model,
                                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                latency_ms=result.latency_ms, provider=result.provider, cost_usd=result.cost_usd)
            await transition_task(
                session, task, TaskStatus.SUCCEEDED, event_type=EventType.TASK_SUCCEEDED,
                extra={"attempt": task.attempt_count, "model": result.model, "provider": result.provider,
                       "cost_usd": result.cost_usd, "fallback_from": result.fallback_from,
                       "latency_ms": result.latency_ms, "input_tokens": result.input_tokens,
                       "output_tokens": result.output_tokens},
            )
            logger.info("task.succeeded key=%s attempt=%d model=%s latency_ms=%d",
                        task.key, task.attempt_count, result.model, result.latency_ms)
        else:
            assert error is not None
            error_type, message, retry_after = error
            await close_attempt(session, task.id, outcome="failed", error_type=error_type, error=message)
            new_status = await fail_or_retry(session, task, settings, error_type=error_type,
                                             error=message, retry_after=retry_after)
            logger.warning("task.attempt_failed key=%s attempt=%d error_type=%s -> %s error=%s",
                           task.key, task.attempt_count, error_type, new_status, message)
        await session.commit()
        return True


async def _budget_return(
    session_factory: async_sessionmaker[AsyncSession], claim: Claim, worker_id: str, limit: str
) -> bool:
    """The budget paused the workflow before this task's call: hand the task back (READY) without
    using up a retry, exactly like an immediate-pause interruption (attempt_count stays the fencing
    token; max_attempts += 1)."""
    async with session_factory() as session:
        task = await session.scalar(select(Task).where(Task.id == claim.task_id).with_for_update())
        if (task is None or task.status != TaskStatus.RUNNING or task.lease_owner != worker_id
                or task.attempt_count != claim.attempt):
            await session.rollback()
            return False
        await close_attempt(session, task.id, outcome="budget_paused", error=f"Budget reached: {limit}")
        task.lease_owner = task.lease_expires_at = None
        task.max_attempts += 1
        await transition_task(session, task, TaskStatus.READY, event_type=EventType.TASK_INTERRUPTED,
                              reason="budget_exceeded", extra={"limit": limit, "attempt": task.attempt_count})
        await session.commit()
    logger.warning("task.budget_paused task=%s limit=%s (returned to READY)", claim.task_id, limit)
    return True
