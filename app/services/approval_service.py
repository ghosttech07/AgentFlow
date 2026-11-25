"""Approval checkpoints (Part 6). Code decides control: a requires_approval task runs only after a
human approved it, and the engine re-checks that at claim time.

Lifecycle of one approval row:
    (scheduler) READY task, no approved row -> pending (task AWAITING_APPROVAL)
    pending --approve--> approved   (task READY; valid for all automatic retries of the task)
    pending --reject---> rejected   (task FAILED 'rejected' + workflow FAILED, or task + descendants SKIPPED)
    pending --timeout--> expired    (task FAILED 'approval_expired' + workflow FAILED)
    pending --cancel---> cancelled  (workflow cancelled)
Lock order everywhere: workflow row, then approval row, then tasks (same as the scheduler).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.registry import sensitive_agent_types
from app.config import Settings
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import ConflictError, NotFoundError
from app.core.events import EventType
from app.core.state_machine import transition_task, transition_workflow
from app.db.base import utcnow
from app.models.approval import Approval
from app.models.task import Task
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 1500
_FAILABLE = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.PAUSED)


def _preview(output: dict[str, Any] | None) -> str:
    text = json.dumps(output or {}, ensure_ascii=False)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + f"... [truncated, {len(text)} chars]"


def build_request_payload(wf: Workflow, task: Task) -> dict[str, Any]:
    """Everything a human needs to decide, frozen at request time. Task deps must be loaded."""
    sensitive = task.agent_type in sensitive_agent_types()
    return {
        "workflow_id": str(wf.id),
        "workflow_goal": wf.goal,
        "task_key": task.key,
        "task_name": task.name,
        "task_description": task.description,
        "agent_type": task.agent_type,
        "why_sensitive": (f"Agent '{task.agent_type}' is marked sensitive: its tasks always need approval."
                          if sensitive else "The task was created with requires_approval=true."),
        "task_input": task.input,
        "dependency_previews": {d.key: _preview(d.output) for d in task.dependencies},
    }


async def approved_task_ids(session: AsyncSession, workflow_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(Approval.task_id).where(Approval.workflow_id == workflow_id, Approval.status == "approved")
    )
    return set(rows.all())


async def request_approval(session: AsyncSession, wf: Workflow, task: Task, settings: Settings) -> Approval:
    """READY gated task -> AWAITING_APPROVAL + a pending approval (event approval.requested)."""
    now = utcnow()
    hours = settings.approval_timeout_hours
    approval = Approval(
        id=uuid.uuid4(), workflow_id=wf.id, task_id=task.id, status="pending", requested_at=now,
        expires_at=now + timedelta(hours=hours) if hours > 0 else None,
        request_payload=build_request_payload(wf, task),
    )
    session.add(approval)
    await transition_task(
        session, task, TaskStatus.AWAITING_APPROVAL, event_type=EventType.APPROVAL_REQUESTED,
        extra={"approval_id": str(approval.id),
               "expires_at": approval.expires_at.isoformat() if approval.expires_at else None},
    )
    logger.info("approval.requested workflow=%s task=%s approval=%s", wf.id, task.key, approval.id)
    return approval


async def expire_due(session: AsyncSession, wf: Workflow, tasks: list[Task]) -> int:
    """Pending approvals past expires_at -> expired; their task FAILED (approval_expired)."""
    rows = (
        await session.scalars(
            select(Approval).where(Approval.workflow_id == wf.id, Approval.status == "pending",
                                   Approval.expires_at.is_not(None), Approval.expires_at <= utcnow())
            .with_for_update()
        )
    ).all()
    by_id = {t.id: t for t in tasks}
    for a in rows:
        a.status, a.decided_at = "expired", utcnow()
        task = by_id[a.task_id]
        task.error_type = "approval_expired"
        task.error = f"Approval {a.id} expired at {a.expires_at.isoformat()} without a decision"
        if task.status == TaskStatus.AWAITING_APPROVAL:
            await transition_task(session, task, TaskStatus.FAILED, event_type=EventType.APPROVAL_EXPIRED,
                                  extra={"approval_id": str(a.id)})
        logger.warning("approval.expired workflow=%s task=%s", wf.id, task.key)
    return len(rows)


# ------------------------------------------------------------------------------ queries


async def list_approvals(
    session: AsyncSession, status: str | None = None, workflow_id: uuid.UUID | None = None
) -> list[Approval]:
    stmt = select(Approval).order_by(Approval.requested_at, Approval.id)
    if status:
        stmt = stmt.where(Approval.status == status)
    if workflow_id:
        stmt = stmt.where(Approval.workflow_id == workflow_id)
    return list((await session.scalars(stmt)).all())


async def get_approval(session: AsyncSession, approval_id: uuid.UUID) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    return approval


# ------------------------------------------------------------------------------ decisions


async def _lock_pending(session: AsyncSession, approval_id: uuid.UUID) -> tuple[Approval, Workflow, Task]:
    """Lock workflow -> approval -> task; 409 unless the approval is still pending and unexpired.

    The row locks serialize concurrent decisions: the second caller sees a decided row and gets 409.
    """
    wf_id = await session.scalar(select(Approval.workflow_id).where(Approval.id == approval_id))
    if wf_id is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    wf = await session.scalar(select(Workflow).where(Workflow.id == wf_id).with_for_update())
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    task = await session.scalar(
        select(Task).where(Task.id == approval.task_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval.status != "pending":
        status = approval.status
        await session.rollback()
        raise ConflictError(f"Approval is already {status}")
    if approval.expires_at is not None and approval.expires_at <= utcnow():
        await session.rollback()
        raise ConflictError("Approval has expired")
    return approval, wf, task


async def approve(
    session: AsyncSession, approval_id: uuid.UUID, *, actor: str, comment: str | None,
    input_overrides: dict[str, Any] | None,
) -> Approval:
    approval, wf, task = await _lock_pending(session, approval_id)
    changes: dict[str, Any] = {}
    if input_overrides:
        old = task.input or {}
        changes = {k: {"from": old.get(k), "to": v} for k, v in input_overrides.items() if old.get(k) != v}
        task.input = {**old, **input_overrides}  # shallow merge; reassign so the JSONB change is saved
    approval.status, approval.decided_at, approval.decided_by = "approved", utcnow(), actor
    approval.comment, approval.input_overrides = comment, input_overrides or None
    extra = {"approval_id": str(approval.id), "actor": actor, "comment": comment, "input_changes": changes}
    if task.status == TaskStatus.AWAITING_APPROVAL:
        await transition_task(session, task, TaskStatus.READY, event_type=EventType.APPROVAL_APPROVED,
                              extra=extra)
    if wf.status == WorkflowStatus.AWAITING_APPROVAL:  # PAUSED stays PAUSED; the decision is kept
        await transition_workflow(session, wf, WorkflowStatus.RUNNING, reason=f"approved by {actor}")
    await session.commit()
    logger.info("approval.approved approval=%s task=%s actor=%s changed=%s", approval.id, task.key, actor,
                sorted(changes))
    return approval


async def reject(
    session: AsyncSession, approval_id: uuid.UUID, *, actor: str, reason: str, on_reject: str
) -> Approval:
    approval, wf, task = await _lock_pending(session, approval_id)
    approval.status, approval.decided_at, approval.decided_by = "rejected", utcnow(), actor
    approval.comment, approval.on_reject = reason, on_reject
    extra = {"approval_id": str(approval.id), "actor": actor, "reason": reason, "on_reject": on_reject}
    if on_reject == "skip_task":
        tasks = (await session.scalars(
            select(Task).where(Task.workflow_id == wf.id).options(selectinload(Task.dependencies))
            .with_for_update()
        )).all()
        await transition_task(session, task, TaskStatus.SKIPPED, event_type=EventType.APPROVAL_REJECTED,
                              extra=extra)
        # Descendants cannot have started (their upstream never ran), so they are PENDING.
        targets = [t for t in _descendants(task, tasks) if t.status == TaskStatus.PENDING]
        for t in targets:
            await transition_task(session, t, TaskStatus.SKIPPED, event_type=EventType.TASK_SKIPPED,
                                  extra={"reason": "upstream_rejected", "upstream": task.key})
        skipped = [t.key for t in targets]
        if wf.status == WorkflowStatus.AWAITING_APPROVAL:
            await transition_workflow(session, wf, WorkflowStatus.RUNNING,
                                      reason=f"task '{task.key}' skipped after rejection")
        logger.info("approval.rejected task=%s skip_task descendants=%s", task.key, skipped)
    else:
        task.error_type = "rejected"
        task.error = f"Rejected by {actor}: {reason}"
        await transition_task(session, task, TaskStatus.FAILED, event_type=EventType.APPROVAL_REJECTED,
                              extra=extra)
        if wf.status in _FAILABLE:
            wf.error = f"Task '{task.key}' failed (rejected): {task.error}"
            await transition_workflow(session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
                                      reason=wf.error, extra={"failed_tasks": [task.key]})
        logger.info("approval.rejected task=%s fail_workflow", task.key)
    await session.commit()
    return approval


def _descendants(root: Task, tasks: list[Task]) -> list[Task]:
    children: dict[uuid.UUID, list[Task]] = {}
    for t in tasks:
        for d in t.dependencies:
            children.setdefault(d.id, []).append(t)
    seen: dict[uuid.UUID, Task] = {}
    stack = list(children.get(root.id, []))
    while stack:
        t = stack.pop()
        if t.id not in seen:
            seen[t.id] = t
            stack.extend(children.get(t.id, []))
    return list(seen.values())
