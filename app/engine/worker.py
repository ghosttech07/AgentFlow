"""Execute one claimed task with its agent.

Rules:
- Own DB session per step; no transaction is open during the LLM call.
- While the agent runs, heartbeat every HEARTBEAT_INTERVAL_SECONDS with a conditional UPDATE
  (status RUNNING, lease_owner = me, attempt_count = my attempt). 0 rows -> ownership lost:
  cancel the agent call and discard its result.
- Idempotency guard: every save is conditional on the same three columns, so a slow "zombie"
  worker can never overwrite a newer attempt (Part 5) or a cancelled task (Part 6).
- Part 9: when the agent has effective tools the worker builds a ToolRuntime and wires it
  onto the agent before the call. ToolApprovalNeeded suspends the task (no retry consumed).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agents import AgentInput, AgentResult, create_agent
from app.config import Settings
from app.core.enums import TaskStatus
from app.core.events import EventType, record_event
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
from app.tools.loop import ToolApprovalNeeded

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
            _run_agent(agent_type, agent_input, llm, session_factory, workflow_id, key, settings, has_chaos,
                       claim.task_id),
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
    except ToolApprovalNeeded as need:  # a tool call needs a human: suspend, free the slot, no retry used
        return await _suspend(session_factory, claim, worker_id, settings, need)
    except Exception as exc:
        error_type = classify_failure(exc)
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        if error_type == "timeout":
            message = f"Task timed out after {_task_timeout(agent_type, settings):g}s"
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
    task_id: uuid.UUID | None = None,
) -> AgentResult:
    async def run() -> AgentResult:
        if has_chaos:  # dev fault injection
            await chaos.inject(session_factory, workflow_id, key, settings)
        agent = create_agent(agent_type, llm)
        agent.session_factory = session_factory  # same DB as the engine (publisher side effects)
        # Part 9: wire a ToolRuntime when the agent has effective tools.
        if task_id is not None:
            agent.tool_runtime = await _build_tool_runtime(agent, session_factory, workflow_id, task_id, key,
                                                           settings, agent_input)
        result = await agent.run(agent_input)
        if agent_input.memory_ids:  # Part 10: a tainted memory taints the reader (memory poisoning stays gated)
            result.memories_used = list(agent_input.memory_ids)
            result.tainted = result.tainted or agent_input.memory_tainted
        return result

    return await asyncio.wait_for(run(), timeout=_task_timeout(agent_type, settings))


def _effective_tools(agent_type: str, settings: Settings) -> list[str]:
    from app.agents import get_agent_class
    from app.tools import config as tools_config

    cfg = tools_config.get_tools_config()
    if cfg is None:
        return []
    return tools_config.effective_tools(agent_type, get_agent_class(agent_type).allowed_tools, cfg, settings)


def _has_tools(agent_type: str, settings: Settings) -> bool:
    return bool(_effective_tools(agent_type, settings))


def _task_timeout(agent_type: str, settings: Settings) -> float:
    """A tool loop makes several model + tool calls: it gets TOOL_LOOP_TIMEOUT_SECONDS (a suspension ends the
    attempt, so time waiting for an approval never counts; a resume starts fresh timers)."""
    return settings.tool_loop_timeout_seconds if _has_tools(agent_type, settings) else settings.task_timeout_seconds


async def _build_tool_runtime(agent: Any, session_factory: async_sessionmaker[AsyncSession],
                              workflow_id: uuid.UUID, task_id: uuid.UUID, key: str, settings: Settings,
                              agent_input: AgentInput | None = None) -> Any:
    """ToolRuntime for an agent with effective tools (None otherwise): the running attempt number, upstream
    taint and the transcript of earlier attempts to resume from (all read from the DB)."""
    import app.tools.catalog  # noqa: F401  (registers every tool)
    from app.models.attempt import TaskAttempt
    from app.tools import config as tools_config
    from app.tools.loop import ToolRuntime, resume_info

    tools = _effective_tools(agent.agent_type, settings)
    if not tools:
        return None
    async with session_factory() as session:
        attempt_number = await session.scalar(
            select(TaskAttempt.attempt_number).where(TaskAttempt.task_id == task_id)
            .order_by(TaskAttempt.attempt_number.desc()).limit(1)) or 1
        task = await session.scalar(select(Task).where(Task.id == task_id).options(selectinload(Task.dependencies)))
        tainted_from = sorted(d.key for d in (task.dependencies if task else []) if d.tainted)
        sources, warnings = await _earlier_untrusted(session, workflow_id, task_id) if tainted_from else ([], [])
        resume = await resume_info(session, task_id, attempt_number)
    return ToolRuntime(session_factory=session_factory, config=tools_config.get_tools_config(), settings=settings,
                       workflow_id=workflow_id, task_id=task_id, task_key=key, attempt_number=attempt_number,
                       tools=tools, tainted_from=tainted_from + (["memory"] if agent_input and agent_input.memory_tainted
                                                                  else []),
                       resume=resume, inherited_sources=sources, inherited_warnings=warnings,
                       private_context=bool(agent_input and agent_input.memory_private),
                       memory_sources=list(agent_input.memory_sources) if agent_input else [])


async def _earlier_untrusted(session: AsyncSession, workflow_id: uuid.UUID,
                             task_id: uuid.UUID) -> tuple[list[str], list[str]]:
    """Untrusted pages read and injection flags raised by OTHER tasks of this workflow (conservative: a human
    approving a send from a tainted task should see everything suspicious the workflow has read)."""
    from app.models.tool_call import ToolCall

    rows = (await session.scalars(select(ToolCall).where(
        ToolCall.workflow_id == workflow_id, ToolCall.task_id != task_id, ToolCall.status == "succeeded",
        ToolCall.tool.in_(("web_search", "web_fetch", "http_request"))).order_by(ToolCall.created_at))).all()
    sources: list[str] = []
    warnings: list[str] = []
    for r in rows:
        args = r.arguments or {}
        src = str(args.get("url") or (f"search: {args['query']}" if "query" in args else r.tool))[:300]
        if r.tool != "web_search" and src not in sources:
            sources.append(src)
        for w in r.injection_warnings or []:
            note = f"{w} ({src})"
            if note not in warnings:
                warnings.append(note)
    return sources[:20], warnings


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
            # Taint flows through dependencies even across tasks without tools (researcher -> writer -> communicator).
            task.tainted = bool(getattr(result, "tainted", False)) or await _upstream_tainted(session, task.id)
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
            if result.memories_used:
                task.metrics = {**(task.metrics or {}), "memories_used": result.memories_used}
                record_event(session, workflow_id=task.workflow_id, task_id=task.id, event_type=EventType.MEMORY_USED,
                             payload={"task_key": task.key, "memory_ids": result.memories_used})
            from app.models.workflow import Workflow as _Wf
            from app.services import memory_service

            wf_row = await session.get(_Wf, task.workflow_id)
            if wf_row is not None:
                await memory_service.write_from_task(session, wf_row, task, settings)
            verified = (result.tool_summary or {}).get("sources_verified")
            if verified is not None:
                record_event(session, workflow_id=task.workflow_id, task_id=task.id,
                             event_type=EventType.TASK_SOURCES_VERIFIED,
                             payload={"task_key": task.key, **verified})
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


async def _upstream_tainted(session: AsyncSession, task_id: uuid.UUID) -> bool:
    from app.models.task import task_dependencies

    return bool(await session.scalar(
        select(Task.id).join(task_dependencies, task_dependencies.c.depends_on_task_id == Task.id)
        .where(task_dependencies.c.task_id == task_id, Task.tainted.is_(True)).limit(1)))


async def _suspend(session_factory: async_sessionmaker[AsyncSession], claim: Claim, worker_id: str,
                   settings: Settings, need: ToolApprovalNeeded) -> bool:
    """RUNNING -> AWAITING_APPROVAL in one guarded transaction: the attempt is closed as `suspended` with the
    loop checkpoint, the lease is released (the worker slot is free while a human decides, which can take
    hours) and max_attempts += 1 so no retry is used up. Waiting time counts toward no timeout."""
    from app.models.workflow import Workflow
    from app.services import approval_service
    from app.services.notify import notify_approval

    async with session_factory() as session:
        task = await session.scalar(select(Task).where(Task.id == claim.task_id).with_for_update())
        if (task is None or task.status != TaskStatus.RUNNING or task.lease_owner != worker_id
                or task.attempt_count != claim.attempt):
            await session.rollback()
            return False
        wf = await session.get(Workflow, task.workflow_id)
        await close_attempt(session, task.id, outcome="suspended", error="waiting for a human decision on a tool call",
                            loop_state=need.state.model_dump(mode="json"))
        task.lease_owner = task.lease_expires_at = None
        task.max_attempts += 1
        approval = await approval_service.request_tool_approval(session, wf, task, need.tool_call_id, need.payload,
                                                                settings)
        await session.commit()
        payload = {"approval_id": str(approval.id), **approval.request_payload}
    notify_approval(settings.approval_webhook_url, payload)
    logger.info("task.suspended key=%s tool_call=%s approval=%s", task.key, need.tool_call_id, approval.id)
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
