"""Workflow business logic: creation (with task graph), queries.

Services own transactions: they commit on success and roll back on failure.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.registry import sensitive_agent_types, task_agent_types
from app.config import get_settings
from app.core.dag import TaskLike, TaskNode, execution_levels, to_mermaid, validate_plan
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.events import EventType, record_event
from app.core.state_machine import transition_task, transition_workflow
from app.db.base import utcnow
from app.models.attempt import TaskAttempt
from app.models.event import WorkflowEvent
from app.models.task import Task
from app.models.workflow import Workflow
from app.schemas.task import TaskCreate
from app.schemas.workflow import WorkflowCreate

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def validate_task_graph(tasks: Sequence[TaskLike]) -> None:
    """Run the DAG rules (app.core.dag.validate_plan). Raises ValidationFailedError listing ALL errors.

    Shared by manual workflow creation and the Planner, so both meet the same bar.
    """
    errors = validate_plan(tasks, task_agent_types(), get_settings().max_plan_tasks)
    if errors:
        messages = [e.message for e in errors]
        raise ValidationFailedError("; ".join(messages), details=messages)


def enforce_sensitive_policy(specs: Sequence[TaskCreate]) -> list[str]:
    """Force requires_approval=True for tasks run by sensitive agents. Returns the forced keys.

    The LLM (or a client) may add approvals elsewhere, but can never switch one off here.
    """
    sensitive = set(sensitive_agent_types())
    forced = [s.key for s in specs if s.agent_type in sensitive and not s.requires_approval]
    for spec in specs:
        if spec.agent_type in sensitive:
            spec.requires_approval = True
    return forced


# --------------------------------------------------------------------------------------
# Shared task-graph persistence helpers (used by create_workflow and the planner)
# --------------------------------------------------------------------------------------


def build_tasks(workflow_id: uuid.UUID, specs: Sequence[TaskCreate]) -> list[Task]:
    """Build Task rows with dependency links from validated specs (not yet added to a session)."""
    # Tiny per-task offsets keep created_at strictly increasing, so tasks are listed in
    # spec order even when the clock has coarse resolution.
    base_time = utcnow()
    tasks_by_key: dict[str, Task] = {}
    for index, spec in enumerate(specs):
        tasks_by_key[spec.key] = Task(
            id=uuid.uuid4(),  # explicit so events can reference it before flush
            workflow_id=workflow_id,
            key=spec.key,
            name=spec.name,
            description=spec.description,
            agent_type=spec.agent_type,
            input=spec.input,
            requires_approval=spec.requires_approval,
            max_attempts=spec.max_attempts,
            created_at=base_time + timedelta(microseconds=index),
            updated_at=base_time,
        )
    for spec in specs:
        tasks_by_key[spec.key].dependencies = [tasks_by_key[k] for k in spec.depends_on]
    return list(tasks_by_key.values())


def record_task_created_events(
    session: AsyncSession, specs: Sequence[TaskCreate], tasks: Sequence[Task]
) -> None:
    """One task.created event per task. Call after the tasks are flushed (FK)."""
    for spec, task in zip(specs, tasks, strict=True):
        record_event(
            session,
            workflow_id=task.workflow_id,
            task_id=task.id,
            event_type=EventType.TASK_CREATED,
            payload={"key": spec.key, "agent_type": spec.agent_type, "depends_on": list(spec.depends_on)},
        )


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


async def create_workflow(session: AsyncSession, data: WorkflowCreate) -> Workflow:
    """Create a workflow, its tasks and dependency edges in ONE transaction, plus events."""
    if data.tasks:  # goal-only workflows (no tasks) are planned later
        validate_task_graph(data.tasks)
        enforce_sensitive_policy(data.tasks)

    context = dict(data.context)
    if data.run and data.constraints:  # kept for the engine's background planner
        context["plan_constraints"] = data.constraints.model_dump(exclude_none=True)
    workflow = Workflow(
        id=uuid.uuid4(), name=data.name, goal=data.goal, context=context, run_requested=data.run,
        budget=data.budget.stored() if data.budget else None,
    )
    tasks = build_tasks(workflow.id, data.tasks)
    workflow.tasks = tasks

    try:
        session.add(workflow)
        record_event(
            session,
            workflow_id=workflow.id,
            event_type=EventType.WORKFLOW_CREATED,
            payload={"name": workflow.name, "task_count": len(data.tasks)},
        )
        # Flush the workflow + tasks first so task.created events (FK to tasks) insert after.
        await session.flush()
        record_task_created_events(session, data.tasks, tasks)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("workflow.create_failed name=%r", data.name)
        raise

    logger.info(
        "workflow.created id=%s name=%r tasks=%d", workflow.id, workflow.name, len(data.tasks)
    )
    return await get_workflow(session, workflow.id)


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------


async def get_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    """Return a workflow with tasks and their dependencies loaded, or raise NotFoundError."""
    stmt = (
        select(Workflow)
        .where(Workflow.id == workflow_id)
        .options(selectinload(Workflow.tasks).selectinload(Task.dependencies),
                 selectinload(Workflow.approvals))
        .execution_options(populate_existing=True)
    )
    workflow = (await session.execute(stmt)).scalar_one_or_none()
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    return workflow


async def list_workflows(
    session: AsyncSession,
    *,
    status: WorkflowStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Workflow], int]:
    """Return (page of workflows newest first, total matching count)."""
    filters = [Workflow.status == status] if status is not None else []

    total = (
        await session.execute(select(func.count()).select_from(Workflow).where(*filters))
    ).scalar_one()
    rows = await session.execute(
        select(Workflow)
        .where(*filters)
        .order_by(Workflow.created_at.desc(), Workflow.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), total


async def list_tasks(session: AsyncSession, workflow_id: uuid.UUID) -> list[Task]:
    """Tasks of a workflow (with dependencies loaded), in creation order."""
    await _ensure_workflow_exists(session, workflow_id)
    rows = await session.execute(
        select(Task)
        .where(Task.workflow_id == workflow_id)
        .options(selectinload(Task.dependencies))
        .order_by(Task.created_at, Task.key)
    )
    return list(rows.scalars().all())


async def list_events(session: AsyncSession, workflow_id: uuid.UUID) -> list[WorkflowEvent]:
    """Events of a workflow in chronological (insertion) order."""
    await _ensure_workflow_exists(session, workflow_id)
    rows = await session.execute(
        select(WorkflowEvent)
        .where(WorkflowEvent.workflow_id == workflow_id)
        .order_by(WorkflowEvent.created_at, WorkflowEvent.id)
    )
    return list(rows.scalars().all())


async def _ensure_workflow_exists(session: AsyncSession, workflow_id: uuid.UUID) -> None:
    exists = await session.scalar(select(Workflow.id).where(Workflow.id == workflow_id))
    if exists is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")


async def get_workflow_graph(session: AsyncSession, workflow_id: uuid.UUID) -> dict[str, Any]:
    """Nodes, edges (dependency -> dependent), execution levels and Mermaid for a workflow."""
    workflow = await get_workflow(session, workflow_id)
    nodes = [
        TaskNode(key=t.key, agent_type=t.agent_type, depends_on=[d.key for d in t.dependencies])
        for t in workflow.tasks
    ]
    return {
        "nodes": [
            {"key": t.key, "name": t.name, "agent_type": t.agent_type, "status": t.status,
             "requires_approval": t.requires_approval}
            for t in workflow.tasks
        ],
        "edges": [{"from": d, "to": n.key} for n in nodes for d in n.depends_on],
        "execution_levels": execution_levels(nodes),
        "mermaid": to_mermaid(nodes),
    }


# --------------------------------------------------------------------------------------
# Execution (Part 4)
# --------------------------------------------------------------------------------------


async def start_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    """PENDING workflow with tasks -> RUNNING (event workflow.started). 409 otherwise."""
    workflow = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    try:
        if workflow.status != WorkflowStatus.PENDING:
            raise ConflictError(_not_startable_reason(workflow.status))
        task_count = await session.scalar(
            select(func.count()).select_from(Task).where(Task.workflow_id == workflow_id)
        )
        if not task_count:
            raise ConflictError(
                "Workflow has no tasks yet: plan it first (POST /workflows/{id}/plan) or create it with run=true"
            )
        await start_workflow_locked(session, workflow)
    except Exception:
        await session.rollback()
        raise
    await session.commit()
    logger.info("workflow.started id=%s", workflow.id)
    return await get_workflow(session, workflow_id)


async def start_workflow_locked(session: AsyncSession, workflow: Workflow) -> None:
    """Transition an already-locked, startable workflow to RUNNING (no commit)."""
    workflow.run_requested = False
    await transition_workflow(session, workflow, WorkflowStatus.RUNNING, event_type=EventType.WORKFLOW_STARTED)


def _not_startable_reason(status: WorkflowStatus) -> str:
    if status in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED, WorkflowStatus.AWAITING_APPROVAL):
        return f"Workflow is already running (status {status})"
    if status == WorkflowStatus.PLANNING:
        return "Workflow is still being planned"
    return f"Workflow is in a terminal state ({status}) and cannot be started"


async def get_task_by_key(session: AsyncSession, workflow_id: uuid.UUID, key: str) -> Task:
    await _ensure_workflow_exists(session, workflow_id)
    task = await session.scalar(
        select(Task).where(Task.workflow_id == workflow_id, Task.key == key)
        .options(selectinload(Task.dependencies))
    )
    if task is None:
        raise NotFoundError(f"Task '{key}' not found in workflow {workflow_id}")
    return task


async def get_result(session: AsyncSession, workflow_id: uuid.UUID) -> dict[str, Any]:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    if workflow.status != WorkflowStatus.COMPLETED:
        raise ConflictError(f"Workflow has no result yet (status {workflow.status})")
    return workflow.result or {}


# --------------------------------------------------------------------------------------
# Reliability (Part 5)
# --------------------------------------------------------------------------------------


async def retry_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    """Retry the failed part of a FAILED workflow (POST /workflows/{id}/retry).

    FAILED tasks -> PENDING with attempt_count reset (attempt history keeps the old rows and new
    attempt numbers continue after them); SUCCEEDED tasks are untouched. A workflow that failed
    during planning (no tasks) is re-planned by the engine instead (run_requested=true).
    """
    workflow = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    if workflow.status != WorkflowStatus.FAILED:
        current = workflow.status  # read before rollback expires the object
        await session.rollback()
        raise ConflictError(f"Only FAILED workflows can be retried (status {current})")
    tasks = (await session.scalars(select(Task).where(Task.workflow_id == workflow_id)
                                   .order_by(Task.created_at))).all()
    workflow.error = None
    if not tasks:
        workflow.run_requested = True  # engine pickup re-plans FAILED + run_requested
        record_event(session, workflow_id=workflow.id, event_type=EventType.WORKFLOW_RETRIED,
                     payload={"replan": True, "reset_tasks": []})
    else:
        reset = []
        for t in tasks:
            if t.status == TaskStatus.FAILED:
                t.attempt_count = 0
                t.error = t.error_type = None
                t.next_attempt_at = None
                await transition_task(session, t, TaskStatus.PENDING, reason="manual retry")
                reset.append(t.key)
        await transition_workflow(session, workflow, WorkflowStatus.RUNNING,
                                  event_type=EventType.WORKFLOW_RETRIED,
                                  extra={"replan": False, "reset_tasks": reset})
    await session.commit()
    logger.info("workflow.retried id=%s replan=%s", workflow.id, not tasks)
    return await get_workflow(session, workflow_id)


async def list_attempts(session: AsyncSession, workflow_id: uuid.UUID, key: str) -> list[TaskAttempt]:
    task = await get_task_by_key(session, workflow_id, key)
    rows = await session.scalars(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id).order_by(TaskAttempt.attempt_number)
    )
    return list(rows.all())



# --------------------------------------------------------------------------------------
# Cost control (Part 8)
# --------------------------------------------------------------------------------------


async def detail_with_usage(session: AsyncSession, workflow: Workflow) -> Any:
    """WorkflowDetail plus budget usage and remaining (one aggregate query over llm_calls)."""
    from app.schemas.workflow import WorkflowDetail
    from app.services import budget_service

    detail = WorkflowDetail.from_model(workflow)
    usage = await budget_service.get_usage(session, workflow)
    detail.usage = usage.as_dict()
    detail.remaining = budget_service.remaining(detail.effective_budget, usage)
    return detail
