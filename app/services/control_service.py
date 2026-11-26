"""Human controls (Part 6): pause, resume, cancel. Each is one transaction on the locked workflow
row, records actor/reason in its event, and is safe to repeat (double clicks) where the spec says so.

The service only changes the DB. It returns the ids of RUNNING tasks it took away from their
workers, and the route asks the local engine to cancel them (Engine.interrupt). Workers in other
processes notice through the ownership guard (status RUNNING + lease_owner + attempt_count) on
their next heartbeat or save, and discard their result.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import ConflictError, NotFoundError
from app.core.events import EventType
from app.core.state_machine import can_transition_workflow, transition_task, transition_workflow
from app.db.base import utcnow
from app.engine.retry import close_attempt
from app.engine.scheduler import can_progress
from app.models.approval import Approval
from app.models.task import Task
from app.models.workflow import Workflow
from app.services.workflow_service import get_workflow

logger = logging.getLogger(__name__)

CANCELLABLE_TASKS = (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRYING,
                     TaskStatus.AWAITING_APPROVAL, TaskStatus.RUNNING)


async def _lock(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    wf = await session.scalar(
        select(Workflow).where(Workflow.id == workflow_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if wf is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    return wf


async def _conflict(session: AsyncSession, message: str) -> ConflictError:
    await session.rollback()
    return ConflictError(message)


async def _tasks(session: AsyncSession, workflow_id: uuid.UUID, *statuses: TaskStatus) -> list[Task]:
    stmt = (select(Task).where(Task.workflow_id == workflow_id).options(selectinload(Task.dependencies))
            .order_by(Task.created_at, Task.key).with_for_update(of=Task))
    if statuses:
        stmt = stmt.where(Task.status.in_(statuses))
    return list((await session.scalars(stmt)).all())


async def pause(
    session: AsyncSession, workflow_id: uuid.UUID, *, actor: str, reason: str | None, mode: str
) -> tuple[Workflow, list[uuid.UUID]]:
    """RUNNING / AWAITING_APPROVAL -> PAUSED. Already PAUSED -> unchanged (idempotent).

    graceful: in-flight tasks finish and save normally. immediate: they go back to READY and their
    attempt is closed as 'interrupted'. attempt_count stays (it is the fencing token the ownership
    guard compares, so it must never go down); max_attempts += 1 instead, so the interruption does
    not use up a retry.
    """
    wf = await _lock(session, workflow_id)
    if wf.status == WorkflowStatus.PAUSED:
        await session.rollback()
        return await get_workflow(session, workflow_id), []
    if not can_transition_workflow(wf.status, WorkflowStatus.PAUSED):
        raise await _conflict(session, f"Only RUNNING or AWAITING_APPROVAL workflows can be paused "
                                       f"(status {wf.status})")
    await transition_workflow(session, wf, WorkflowStatus.PAUSED, event_type=EventType.WORKFLOW_PAUSED,
                              reason=reason, extra={"mode": mode, "actor": actor})
    interrupted: list[uuid.UUID] = []
    if mode == "immediate":
        for t in await _tasks(session, workflow_id, TaskStatus.RUNNING):
            await close_attempt(session, t.id, outcome="interrupted", error=f"Interrupted by {actor} (pause)")
            owner = t.lease_owner
            t.lease_owner = t.lease_expires_at = None
            t.max_attempts += 1
            await transition_task(session, t, TaskStatus.READY, event_type=EventType.TASK_INTERRUPTED,
                                  reason=reason, extra={"actor": actor, "attempt": t.attempt_count,
                                                        "previous_owner": owner})
            interrupted.append(t.id)
    await session.commit()
    logger.info("workflow.paused id=%s mode=%s actor=%s interrupted=%d", wf.id, mode, actor, len(interrupted))
    return await get_workflow(session, workflow_id), interrupted


async def resume(session: AsyncSession, workflow_id: uuid.UUID, *, actor: str) -> Workflow:
    """PAUSED -> RUNNING, or AWAITING_APPROVAL if only pending approvals block it. Continues from DB state."""
    wf = await _lock(session, workflow_id)
    if wf.status != WorkflowStatus.PAUSED:
        raise await _conflict(session, f"Only PAUSED workflows can be resumed (status {wf.status})")
    tasks = await _tasks(session, workflow_id)
    waiting = any(t.status == TaskStatus.AWAITING_APPROVAL for t in tasks)
    target = (WorkflowStatus.AWAITING_APPROVAL if waiting and not can_progress(tasks, utcnow())
              else WorkflowStatus.RUNNING)
    await transition_workflow(session, wf, target, event_type=EventType.WORKFLOW_RESUMED,
                              extra={"actor": actor})
    await session.commit()
    logger.info("workflow.resumed id=%s to=%s actor=%s", wf.id, target, actor)
    return await get_workflow(session, workflow_id)


async def cancel(
    session: AsyncSession, workflow_id: uuid.UUID, *, actor: str, reason: str | None
) -> tuple[Workflow, list[uuid.UUID]]:
    """Cancel everything not finished, in one transaction. Already CANCELLED -> unchanged."""
    wf = await _lock(session, workflow_id)
    if wf.status == WorkflowStatus.CANCELLED:
        await session.rollback()
        return await get_workflow(session, workflow_id), []
    if not can_transition_workflow(wf.status, WorkflowStatus.CANCELLED):
        raise await _conflict(session, f"Workflow cannot be cancelled (status {wf.status})")

    running: list[uuid.UUID] = []
    tasks = await _tasks(session, workflow_id, *CANCELLABLE_TASKS)
    for t in tasks:
        if t.status == TaskStatus.RUNNING:
            await close_attempt(session, t.id, outcome="cancelled", error=f"Cancelled by {actor}")
            running.append(t.id)
        t.lease_owner = t.lease_expires_at = t.next_attempt_at = None
        await transition_task(session, t, TaskStatus.CANCELLED, event_type=EventType.TASK_CANCELLED,
                              reason=reason, extra={"actor": actor})
    approvals = (await session.scalars(
        select(Approval).where(Approval.workflow_id == workflow_id, Approval.status == "pending")
        .with_for_update()
    )).all()
    for a in approvals:
        a.status, a.decided_at, a.decided_by = "cancelled", utcnow(), actor
    if approvals:  # Part 9: a tool call waiting for a human is denied (it never runs)
        from sqlalchemy import update as sa_update

        from app.models.tool_call import ToolCall

        await session.execute(sa_update(ToolCall).where(
            ToolCall.workflow_id == workflow_id, ToolCall.status == "awaiting_approval").values(
            status="denied", error=f"cancelled by {actor}", finished_at=utcnow()))

    wf.run_requested = False  # a cancelled workflow is never picked up or planned again
    wf.error = f"cancelled by {actor}: {reason or 'no reason given'}"
    await transition_workflow(
        session, wf, WorkflowStatus.CANCELLED, event_type=EventType.WORKFLOW_CANCELLED, reason=reason,
        extra={"actor": actor, "cancelled_tasks": len(tasks), "running_tasks_stopped": len(running),
               "cancelled_approvals": len(approvals)},
    )
    await session.commit()
    logger.info("workflow.cancelled id=%s actor=%s tasks=%d running=%d", wf.id, actor, len(tasks), len(running))
    return await get_workflow(session, workflow_id), running
