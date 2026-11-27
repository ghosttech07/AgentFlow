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
    TASK_SOURCES_VERIFIED = "task.sources_verified"
    TASK_CONDITION_EVALUATED = "task.condition_evaluated"  # Part 10: run_if / replan_if with the values used
    WORKFLOW_COMPLETED_WITH_FAILED_BRANCHES = "workflow.completed_with_failed_branches"
    LOOP_CONDITION_EVALUATED = "loop.condition_evaluated"
    LOOP_ITERATION_STARTED = "loop.iteration_started"
    LOOP_COMPLETED = "loop.completed"
    LOOP_EXHAUSTED = "loop.exhausted"
    WORKFLOW_REPLAN_STARTED = "workflow.replan_started"
    WORKFLOW_REPLAN_APPLIED = "workflow.replan_applied"
    WORKFLOW_REPLAN_FAILED = "workflow.replan_failed"
    MEMORY_USED = "memory.used"
    MEMORY_DELETED = "memory.deleted"  # Part 9: researcher citations checked by code
    TOOL_INJECTION_SUSPECTED = "tool.injection_suspected"  # Part 9: suspicious text in an untrusted result
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_VOTE_RECORDED = "approval.vote_recorded"
    APPROVAL_UNSATISFIABLE = "approval.unsatisfiable"  # not enough eligible approvers (escalated at once)
    APPROVAL_ESCALATED = "approval.escalated"  # Part 11: one of several required approvers said yes
    # Cost control (Part 8)
    WORKFLOW_BUDGET_EXCEEDED = "workflow.budget_exceeded"
    WORKFLOW_BUDGET_UPDATED = "workflow.budget_updated"


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
