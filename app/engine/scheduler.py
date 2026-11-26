"""Scheduling steps. Each step is its own short DB transaction; the DB is the source of truth.

Per tick (see Engine.tick):
  pickup_run_requests  - start (or begin planning) workflows created with run=true
  schedule_workflow    - per RUNNING / AWAITING_APPROVAL workflow (never PAUSED): expire approvals /
                         fail / complete / promote / request approvals / RUNNING<->AWAITING_APPROVAL
  claim_tasks          - READY -> RUNNING with FOR UPDATE SKIP LOCKED + lease fields; a
                         requires_approval task only with an approved approval row
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.core.conditions import ConditionError, ConditionSyntaxError, parse_condition
from app.core.dag import TaskNode, leaves
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.events import EventType, record_event
from app.core.joins import TOLERANT_JOINS, decide
from app.core.state_machine import transition_task, transition_workflow
from app.db.base import utcnow
from app.engine.retry import open_attempt, reclaim_lost_task
from app.models.approval import Approval
from app.models.task import Task
from app.models.workflow import Workflow
from app.services import approval_service, budget_service, planning_service
from app.services.notify import notify_approval
from app.services.workflow_service import start_workflow_locked

logger = logging.getLogger(__name__)

DONE_OK = frozenset({TaskStatus.SUCCEEDED, TaskStatus.SKIPPED, TaskStatus.SUPERSEDED})
# Workflows the scheduler works on. PAUSED is deliberately absent: nothing is promoted, gated or
# claimed while paused (the reaper still runs).
SCHEDULED = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL)


async def pickup_run_requests(
    session_factory: async_sessionmaker[AsyncSession], limit: int = 10
) -> tuple[int, list[uuid.UUID]]:
    """Workflows with run_requested=true and PENDING: start those with tasks, begin planning the rest.

    Rows are claimed with SKIP LOCKED, and planning moves them to PLANNING in the same
    transaction, so a workflow is never planned twice. Returns (started, ids_to_plan).
    """
    async with session_factory() as session:
        workflows = (
            await session.scalars(
                select(Workflow)
                .where(
                    Workflow.run_requested.is_(True),
                    # FAILED = planning failed and POST /retry asked for a re-plan
                    Workflow.status.in_([WorkflowStatus.PENDING, WorkflowStatus.FAILED]),
                )
                .order_by(Workflow.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        started, to_plan = 0, []
        for wf in workflows:
            task_count = await session.scalar(
                select(func.count()).select_from(Task).where(Task.workflow_id == wf.id)
            )
            if task_count and wf.status == WorkflowStatus.FAILED:
                wf.run_requested = False  # only unplanned FAILED workflows are re-planned here
            elif task_count:
                await start_workflow_locked(session, wf)
                started += 1
            else:
                await planning_service.begin_planning(
                    session, wf, planning_service.stored_constraints(wf)
                )
                to_plan.append(wf.id)
        await session.commit()
    return started, to_plan


async def schedule_running_workflows(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None
) -> int:
    """Run schedule_workflow for every RUNNING / AWAITING_APPROVAL workflow. Returns #changes."""
    async with session_factory() as session:
        ids = (
            await session.scalars(
                select(Workflow.id).where(Workflow.status.in_(SCHEDULED)).order_by(Workflow.started_at)
            )
        ).all()
    changes = 0
    for workflow_id in ids:
        async with session_factory() as session:
            changes += await schedule_workflow(session, workflow_id, settings)
    return changes


def can_progress(tasks: list[Task], now: datetime) -> bool:
    """True if something runs or can run now: READY/RUNNING, a due retry, a promotable PENDING task."""
    for t in tasks:
        if t.status in (TaskStatus.READY, TaskStatus.RUNNING):
            return True
        if t.status == TaskStatus.RETRYING and (t.next_attempt_at is None or t.next_attempt_at <= now):
            return True
        if t.status == TaskStatus.PENDING and decide(t)[0] != "wait":
            return True
    return False


async def schedule_workflow(
    session: AsyncSession, workflow_id: uuid.UUID, settings: Settings | None = None
) -> int:
    """One scheduling pass over a RUNNING / AWAITING_APPROVAL workflow, in one transaction."""
    settings = settings or get_settings()
    wf = await session.scalar(
        select(Workflow)
        .where(Workflow.id == workflow_id, Workflow.status.in_(SCHEDULED))
        .with_for_update(skip_locked=True)  # another engine (or a control request) holds it
    )
    if wf is None:
        await session.rollback()
        return 0
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.workflow_id == workflow_id)
                .options(selectinload(Task.dependencies))
                .order_by(Task.created_at, Task.key)
            )
        ).all()
    )

    # Expired approvals fail their task (approval_expired); the failure rule below fails the workflow.
    changes = await approval_service.expire_due(session, wf, tasks)

    # Failure: stop scheduling, but leave PENDING tasks untouched (Part 5 resumes them)
    # and let running siblings finish and save their output.
    failed = [t for t in tasks if t.status == TaskStatus.FAILED and not tolerated_failure(t, tasks)]
    if failed:
        first = failed[0]
        wf.error = f"Task '{first.key}' failed ({first.error_type or 'unknown'}): {first.error}"
        await transition_workflow(
            session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
            reason=wf.error, extra={"failed_tasks": [t.key for t in failed]},
        )
        await session.commit()
        logger.warning("workflow.failed id=%s failed_tasks=%s", wf.id, [t.key for t in failed])
        return changes + 1

    if tasks and all(t.status in DONE_OK or (t.status == TaskStatus.FAILED and tolerated_failure(t, tasks))
                     for t in tasks):
        if wf.status == WorkflowStatus.AWAITING_APPROVAL:  # not reachable normally; keep it valid anyway
            await transition_workflow(session, wf, WorkflowStatus.RUNNING)
        await _complete(session, wf, tasks)
        await session.commit()
        return changes + 1

    # Budget (Part 8): a reached limit pauses the workflow here, after the failure and
    # completion rules (a finished workflow is never paused) and before anything is promoted or claimed (claim_tasks only takes READY tasks of RUNNING workflows).
    eff = budget_service.effective_budget(wf.budget, settings)
    usage = await budget_service.get_usage(session, wf)
    if (limit := budget_service.exceeded(eff, usage)) is not None:
        await budget_service.pause_locked(session, wf, limit, usage, eff)
        await session.commit()
        return 1

    now = utcnow()
    for t in tasks:
        # Promote PENDING tasks per their join rule (Part 10; default: all dependencies succeeded), then run_if.
        if t.status == TaskStatus.PENDING:
            changes += await _promote(session, t)
        # Retry: a RETRYING task whose backoff has elapsed becomes READY again.
        elif t.status == TaskStatus.RETRYING and (t.next_attempt_at is None or t.next_attempt_at <= now):
            t.next_attempt_at = None
            await transition_task(session, t, TaskStatus.READY, event_type=EventType.TASK_READY,
                                  extra={"retry": True, "next_attempt": t.attempt_count + 1})
            changes += 1

    # Approval gate: a READY task that needs approval and has none approved waits for a human.
    # An approval stays valid for automatic retries / crash recovery of the same task.
    requested: list[Approval] = []
    gated = [t for t in tasks if t.status == TaskStatus.READY and t.requires_approval]
    if gated:
        approved = await approval_service.approved_task_ids(session, wf.id)
        for t in gated:
            if t.id not in approved:
                requested.append(await approval_service.request_approval(session, wf, t, settings))
                changes += 1

    # Independent branches keep running; the workflow is AWAITING_APPROVAL only while nothing
    # can run and at least one approval is pending.
    waiting = any(t.status == TaskStatus.AWAITING_APPROVAL for t in tasks)
    if wf.status == WorkflowStatus.RUNNING and waiting and not can_progress(tasks, now):
        await transition_workflow(
            session, wf, WorkflowStatus.AWAITING_APPROVAL, event_type=EventType.WORKFLOW_AWAITING_APPROVAL,
            extra={"tasks": [t.key for t in tasks if t.status == TaskStatus.AWAITING_APPROVAL]},
        )
        changes += 1
        logger.info("workflow.awaiting_approval id=%s", wf.id)
    elif wf.status == WorkflowStatus.AWAITING_APPROVAL and (not waiting or can_progress(tasks, now)):
        await transition_workflow(session, wf, WorkflowStatus.RUNNING, reason="work can run again")
        changes += 1

    await session.commit()
    for a in requested:  # after commit: the approval exists before anyone is told about it
        notify_approval(settings.approval_webhook_url, {"approval_id": str(a.id), **a.request_payload})
    return changes


def tolerated_failure(task: Task, tasks: list[Task]) -> bool:
    """A failed task is tolerated when every task that depends on it accepts a failed dependency (Part 10)."""
    dependents = [t for t in tasks if any(d.id == task.id for d in t.dependencies)]
    return bool(dependents) and all((t.join or "all_succeeded") in TOLERANT_JOINS for t in dependents)


def condition_outputs(deps: list[Task]) -> dict[str, dict[str, Any]]:
    return {d.key: {"output": d.output, "status": str(d.status)} for d in deps}


async def _promote(session: AsyncSession, t: Task) -> int:
    decision, reason = decide(t)
    if decision == "wait":
        return 0
    if decision == "skip":
        t.skip_reason = reason
        await transition_task(session, t, TaskStatus.SKIPPED, event_type=EventType.TASK_SKIPPED, reason=reason)
        return 1
    if t.run_if:
        try:
            ok, values = parse_condition(t.run_if).evaluate(condition_outputs(list(t.dependencies)))
        except (ConditionError, ConditionSyntaxError) as exc:
            record_event(session, workflow_id=t.workflow_id, task_id=t.id, event_type=EventType.TASK_CONDITION_EVALUATED,
                         payload={"task_key": t.key, "expression": t.run_if, "error": str(exc)})
            t.error_type, t.error = "condition_error", f"run_if could not be evaluated: {exc}"
            await transition_task(session, t, TaskStatus.FAILED, event_type=EventType.TASK_FAILED,
                                  reason="condition_error")
            return 1
        record_event(session, workflow_id=t.workflow_id, task_id=t.id, event_type=EventType.TASK_CONDITION_EVALUATED,
                     payload={"task_key": t.key, "expression": t.run_if, "result": ok, "values": values})
        if not ok:
            t.skip_reason = "condition_false"
            await transition_task(session, t, TaskStatus.SKIPPED, event_type=EventType.TASK_SKIPPED,
                                  reason="condition_false")
            return 1
    await transition_task(session, t, TaskStatus.READY, event_type=EventType.TASK_READY)
    return 1


async def _complete(session: AsyncSession, wf: Workflow, tasks: list[Task]) -> None:
    nodes = [TaskNode(key=t.key, agent_type=t.agent_type, depends_on=[d.key for d in t.dependencies])
             for t in tasks]
    by_key = {t.key: t for t in tasks}
    tokens: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    for t in tasks:
        for k in tokens:
            tokens[k] += int((t.metrics or {}).get(k) or 0)
    failed_branches = [{"key": t.key, "error_type": t.error_type, "error": t.error}
                       for t in tasks if t.status == TaskStatus.FAILED]  # only tolerated ones reach completion
    wf.result = {
        "final_outputs": {k: by_key[k].output for k in leaves(nodes) if by_key[k].status == TaskStatus.SUCCEEDED},
        "summary": (wf.context or {}).get("plan_summary"),
        "skipped": [t.key for t in tasks if t.status == TaskStatus.SKIPPED],
        **({"failed_branches": failed_branches} if failed_branches else {}),
    }
    if failed_branches:  # completed, but must not look clean
        record_event(session, workflow_id=wf.id, event_type=EventType.WORKFLOW_COMPLETED_WITH_FAILED_BRANCHES,
                     payload={"failed_branches": failed_branches})
    duration = (utcnow() - wf.started_at).total_seconds() if wf.started_at else None
    await transition_workflow(
        session, wf, WorkflowStatus.COMPLETED, event_type=EventType.WORKFLOW_COMPLETED,
        extra={"duration_seconds": round(duration, 2) if duration is not None else None,
               "task_count": len(tasks), **tokens},
    )
    logger.info("workflow.completed id=%s tasks=%d duration_s=%.1f tokens=%s",
                wf.id, len(tasks), duration or 0, tokens)


class Claim(NamedTuple):
    """What a worker needs to own a task: the id and the attempt it was claimed for."""

    task_id: uuid.UUID
    attempt: int


async def claim_tasks(
    session: AsyncSession, worker_id: str, limit: int, lease_seconds: float
) -> list[Claim]:
    """Claim up to `limit` READY tasks of RUNNING workflows, atomically.

    FOR UPDATE SKIP LOCKED: concurrent claimers (other coroutines or engine processes) skip rows
    another transaction holds, so a task is never claimed twice. The attempt history row is
    inserted in the same transaction.
    """
    if limit <= 0:
        return []
    tasks = (
        await session.scalars(
            select(Task)
            .join(Workflow, Task.workflow_id == Workflow.id)
            .where(
                Task.status == TaskStatus.READY,
                Workflow.status == WorkflowStatus.RUNNING,
                # Claim-time double check: never trust the task status alone for gated tasks.
                or_(Task.requires_approval.is_(False),
                    exists().where(Approval.task_id == Task.id, Approval.status == "approved",
                                   Approval.kind == "task")),
            )
            .order_by(Workflow.started_at, Task.created_at, Task.key)
            .limit(limit)
            .with_for_update(of=Task, skip_locked=True)
        )
    ).all()
    expires = utcnow() + timedelta(seconds=lease_seconds)
    claims: list[Claim] = []
    for t in tasks:
        t.attempt_count += 1
        t.lease_owner = worker_id
        t.lease_expires_at = expires
        t.last_heartbeat_at = None
        t.next_attempt_at = None
        row = await open_attempt(session, t, worker_id)
        await transition_task(
            session, t, TaskStatus.RUNNING, event_type=EventType.TASK_STARTED,
            extra={"attempt": t.attempt_count, "attempt_number": row.attempt_number,
                   "worker_id": worker_id},
        )
        claims.append(Claim(t.id, t.attempt_count))
    await session.commit()
    return claims


async def reap_expired_leases(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """RUNNING tasks whose lease expired (worker died or hung): worker_lost -> retry policy."""
    async with session_factory() as session:
        tasks = (
            await session.scalars(
                select(Task)
                .where(Task.status == TaskStatus.RUNNING, Task.lease_expires_at < utcnow())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for t in tasks:
            logger.warning("task.lease_expired key=%s owner=%s attempt=%d", t.key, t.lease_owner, t.attempt_count)
            await reclaim_lost_task(session, t, settings, reason="lease expired")
        await session.commit()
    return len(tasks)


async def next_retry_due(session_factory: async_sessionmaker[AsyncSession]) -> datetime | None:
    """Earliest next_attempt_at of a RETRYING task in a scheduled workflow (engine wake-up time)."""
    async with session_factory() as session:
        return await session.scalar(
            select(func.min(Task.next_attempt_at))
            .join(Workflow, Task.workflow_id == Workflow.id)
            .where(Task.status == TaskStatus.RETRYING, Workflow.status.in_(SCHEDULED))
        )


def task_metrics(result: Any) -> dict[str, Any]:
    """Metrics stored on a task from an AgentResult."""
    return {
        "provider": getattr(result, "provider", None),
        "cost_usd": getattr(result, "cost_usd", None),
        "fallback_from": getattr(result, "fallback_from", None),
        "route_position": getattr(result, "route_position", 0),
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "sources": [s.model_dump() for s in result.sources],
        **({"tools": result.tool_summary} if getattr(result, "tool_summary", None) else {}),
    }
