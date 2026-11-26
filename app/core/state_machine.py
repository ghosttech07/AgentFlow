"""The single place where workflow and task statuses change.

Every status change in AgentFlow MUST go through `transition_workflow` / `transition_task`.
They validate the move, maintain lifecycle timestamps, and append ONE event, all in the
caller's transaction (they flush but never commit). The event type defaults to
`*.status_changed`; callers may name it (e.g. `task.started`); the payload always has from/to.

Workflow transitions
--------------------
| From              | Allowed to                                     | Why                                     |
|-------------------|------------------------------------------------|-----------------------------------------|
| PENDING           | PLANNING, RUNNING, FAILED, CANCELLED           | plan (P3), run a DAG, abort             |
| PLANNING          | PENDING, RUNNING, FAILED, CANCELLED            | plan stored / planning failed / cancel  |
| RUNNING           | PAUSED, AWAITING_APPROVAL, COMPLETED, FAILED,  | pause (P6), only approvals left (P6),   |
|                   | CANCELLED                                      | normal outcomes, cancel                 |
| PAUSED            | RUNNING, AWAITING_APPROVAL, FAILED, CANCELLED  | resume (to AWAITING_APPROVAL if only    |
|                   |                                                | approvals block it), reject, cancel (P6)|
| AWAITING_APPROVAL | RUNNING, PAUSED, FAILED, CANCELLED             | work runnable again / pause / rejected  |
|                   |                                                | or expired / cancel (P6)                |
| COMPLETED         | (none - terminal)                              |                                         |
| FAILED            | RUNNING, PLANNING, CANCELLED                   | EXCEPTION: manual retry (P5), re-plan   |
|                   |                                                | (P3), close a failed workflow (P6)      |
| CANCELLED         | (none - terminal)                              |                                         |

Task transitions
----------------
| From              | Allowed to                                       | Why                                     |
|-------------------|--------------------------------------------------|-----------------------------------------|
| PENDING           | READY, SKIPPED, CANCELLED                        | deps done / upstream rejected / cancel  |
| READY             | RUNNING, AWAITING_APPROVAL, SKIPPED, CANCELLED   | claimed / needs approval / cancel       |
| AWAITING_APPROVAL | READY, FAILED, SKIPPED, CANCELLED                | approved / rejected or expired (P6)     |
| RUNNING           | SUCCEEDED, FAILED, RETRYING, READY,              | attempt outcome; READY = interrupted by |
|                   | AWAITING_APPROVAL, CANCELLED                     | an immediate pause (P6); AWAITING_APPRO-|
|                   |                                                  | VAL = a tool call needs a human (P9)    |
| RETRYING          | READY, RUNNING, FAILED, CANCELLED                | backoff elapsed / retries exhausted (P5)|
| SUCCEEDED         | (none - terminal)                                |                                         |
| FAILED            | RETRYING, PENDING                                | EXCEPTION: retry / recovery (P5)        |
| CANCELLED         | (none - terminal)                                |                                         |
| SKIPPED           | (none - terminal)                                |                                         |

Timestamps
----------
* `started_at` is set the first time an entity enters RUNNING (never overwritten).
* `completed_at` is set on entering a finished state (workflow: COMPLETED/FAILED/CANCELLED;
  task: SUCCEEDED/FAILED/CANCELLED/SKIPPED) and cleared when leaving FAILED again.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import InvalidTransitionError
from app.core.events import EventType, record_event
from app.db.base import utcnow
from app.models.event import WorkflowEvent
from app.models.task import Task
from app.models.workflow import Workflow

W = WorkflowStatus
T = TaskStatus

WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    W.PENDING: frozenset({W.PLANNING, W.RUNNING, W.FAILED, W.CANCELLED}),
    W.PLANNING: frozenset({W.PENDING, W.RUNNING, W.FAILED, W.CANCELLED}),
    W.RUNNING: frozenset({W.PAUSED, W.AWAITING_APPROVAL, W.COMPLETED, W.FAILED, W.CANCELLED}),
    W.PAUSED: frozenset({W.RUNNING, W.AWAITING_APPROVAL, W.FAILED, W.CANCELLED}),
    W.AWAITING_APPROVAL: frozenset({W.RUNNING, W.PAUSED, W.FAILED, W.CANCELLED}),
    W.COMPLETED: frozenset(),
    # Exception to terminality: resume, or re-plan (planning_service only allows it with 0 tasks).
    W.FAILED: frozenset({W.RUNNING, W.PLANNING, W.CANCELLED}),
    W.CANCELLED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    T.PENDING: frozenset({T.READY, T.SKIPPED, T.CANCELLED}),
    T.READY: frozenset({T.RUNNING, T.AWAITING_APPROVAL, T.SKIPPED, T.CANCELLED}),
    T.AWAITING_APPROVAL: frozenset({T.READY, T.FAILED, T.SKIPPED, T.CANCELLED}),
    # AWAITING_APPROVAL (Part 9): suspended mid tool loop until a human decides on one tool call.
    T.RUNNING: frozenset({T.SUCCEEDED, T.FAILED, T.RETRYING, T.READY, T.AWAITING_APPROVAL, T.CANCELLED}),
    T.RETRYING: frozenset({T.READY, T.RUNNING, T.FAILED, T.CANCELLED}),
    T.SUCCEEDED: frozenset(),
    T.FAILED: frozenset({T.RETRYING, T.PENDING}),  # exception to terminality: retry/recovery
    T.CANCELLED: frozenset(),
    T.SKIPPED: frozenset(),
}

# States that end an entity's run (completed_at is stamped on entry).
WORKFLOW_FINISHED_STATES = frozenset({W.COMPLETED, W.FAILED, W.CANCELLED})
TASK_FINISHED_STATES = frozenset({T.SUCCEEDED, T.FAILED, T.CANCELLED, T.SKIPPED})

# Truly terminal states: no outgoing transitions at all.
WORKFLOW_TERMINAL_STATES = frozenset(s for s, nxt in WORKFLOW_TRANSITIONS.items() if not nxt)
TASK_TERMINAL_STATES = frozenset(s for s, nxt in TASK_TRANSITIONS.items() if not nxt)


def can_transition_workflow(from_status: WorkflowStatus, to_status: WorkflowStatus) -> bool:
    """True if a workflow may move from `from_status` to `to_status`."""
    return WorkflowStatus(to_status) in WORKFLOW_TRANSITIONS[WorkflowStatus(from_status)]


def allowed_controls(status: WorkflowStatus) -> dict[str, bool]:
    """Which control actions are allowed now (GET /workflows/{id} `controls`), from the table above."""
    status = WorkflowStatus(status)
    return {
        "can_pause": can_transition_workflow(status, W.PAUSED),
        "can_resume": status is W.PAUSED,  # the only state a resume starts from
        "can_cancel": can_transition_workflow(status, W.CANCELLED),
        "can_retry": status is W.FAILED and can_transition_workflow(status, W.RUNNING),
    }


def can_transition_task(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """True if a task may move from `from_status` to `to_status`."""
    return TaskStatus(to_status) in TASK_TRANSITIONS[TaskStatus(from_status)]


async def transition_workflow(
    session: AsyncSession,
    workflow: Workflow,
    to_status: WorkflowStatus,
    *,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
    event_type: EventType | str = EventType.WORKFLOW_STATUS_CHANGED,
) -> WorkflowEvent:
    """Validate and apply a workflow status change, update timestamps and record an event.

    Raises InvalidTransitionError if the move is not allowed. Flushes, does not commit.
    """
    # Concurrency: callers lock the row (SELECT ... FOR UPDATE) before changing it.
    from_status = WorkflowStatus(workflow.status)
    to_status = WorkflowStatus(to_status)
    if not can_transition_workflow(from_status, to_status):
        raise InvalidTransitionError("workflow", from_status.value, to_status.value)

    now = utcnow()
    workflow.status = to_status
    if to_status is W.RUNNING and workflow.started_at is None:
        workflow.started_at = now
    if to_status in WORKFLOW_FINISHED_STATES:
        workflow.completed_at = now
    elif from_status in WORKFLOW_FINISHED_STATES:
        workflow.completed_at = None  # resumed after FAILED

    event = record_event(
        session,
        workflow_id=workflow.id,
        event_type=event_type,
        payload=_payload(from_status, to_status, reason, extra),
    )
    await session.flush()
    return event


async def transition_task(
    session: AsyncSession,
    task: Task,
    to_status: TaskStatus,
    *,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
    event_type: EventType | str = EventType.TASK_STATUS_CHANGED,
) -> WorkflowEvent:
    """Validate and apply a task status change, update timestamps and record an event.

    Raises InvalidTransitionError if the move is not allowed. Flushes, does not commit.
    """
    from_status = TaskStatus(task.status)
    to_status = TaskStatus(to_status)
    if not can_transition_task(from_status, to_status):
        raise InvalidTransitionError("task", from_status.value, to_status.value)

    now = utcnow()
    task.status = to_status
    if to_status is T.RUNNING and task.started_at is None:
        task.started_at = now
    if to_status in TASK_FINISHED_STATES:
        task.completed_at = now
    elif from_status in TASK_FINISHED_STATES:
        task.completed_at = None  # FAILED -> RETRYING/PENDING

    event = record_event(
        session,
        workflow_id=task.workflow_id,
        task_id=task.id,
        event_type=event_type,
        payload={"task_key": task.key, **_payload(from_status, to_status, reason, extra)},
    )
    await session.flush()
    return event


def _payload(
    from_status: str, to_status: str, reason: str | None, extra: dict[str, Any] | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"from": str(from_status), "to": str(to_status)}
    if reason:
        payload["reason"] = reason
    if extra:
        payload.update(extra)
    return payload
