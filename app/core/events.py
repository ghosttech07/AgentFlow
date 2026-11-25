"""Helpers for appending to the workflow event log (the audit trail / timeline).

Events are added to the caller's session and committed together with the state change they
describe, so the log can never disagree with the persisted state.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import WorkflowEvent


class EventType(StrEnum):
    """Known event types. Use dotted `<entity>.<what>` names."""

    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_STATUS_CHANGED = "workflow.status_changed"
    TASK_CREATED = "task.created"
    TASK_STATUS_CHANGED = "task.status_changed"
    WORKFLOW_PLANNING_STARTED = "workflow.planning_started"
    WORKFLOW_PLAN_CREATED = "workflow.plan_created"
    WORKFLOW_PLANNING_FAILED = "workflow.planning_failed"
    # Engine (Part 4)
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    WORKFLOW_AWAITING_APPROVAL = "workflow.awaiting_approval"
    TASK_READY = "task.ready"
    TASK_STARTED = "task.started"
    TASK_SUCCEEDED = "task.succeeded"
    TASK_FAILED = "task.failed"
    TASK_AWAITING_APPROVAL = "task.awaiting_approval"
    # Reliability (Part 5)
    TASK_RETRY_SCHEDULED = "task.retry_scheduled"
    TASK_LEASE_EXPIRED = "task.lease_expired"
    WORKFLOW_RETRIED = "workflow.retried"
    WORKFLOW_PLANNING_RECOVERED = "workflow.planning_recovered"
    ENGINE_RECOVERED = "engine.recovered"
    # Human controls (Part 6)
    WORKFLOW_PAUSED = "workflow.paused"
    WORKFLOW_RESUMED = "workflow.resumed"
    WORKFLOW_CANCELLED = "workflow.cancelled"
    TASK_INTERRUPTED = "task.interrupted"
    TASK_CANCELLED = "task.cancelled"
    TASK_SKIPPED = "task.skipped"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"


def record_event(
    session: AsyncSession,
    *,
    workflow_id: uuid.UUID,
    event_type: EventType | str,
    payload: dict[str, Any] | None = None,
    task_id: uuid.UUID | None = None,
) -> WorkflowEvent:
    """Add an event to the session (no commit). The caller owns the transaction."""
    event = WorkflowEvent(
        workflow_id=workflow_id,
        task_id=task_id,
        event_type=str(event_type),
        payload=payload or {},
    )
    session.add(event)
    return event
