"""Planning: the Planner proposes a task DAG, code validates/repairs it, then it is stored.

plan()           -> pure planning (LLM + validation + repair + sensitive policy), no DB.
plan_workflow()  -> claim workflow, plan, persist in one transaction, update status.

Workflow status flow:
  PENDING|FAILED(no tasks) -> PLANNING (committed BEFORE the slow LLM call)
     success -> tasks + edges + events stored, PLANNING -> PENDING (planned, ready for Part 4)
     failure -> PLANNING -> FAILED, workflow.error set, "workflow.planning_failed" event

LLM errors propagate unchanged (no retries here; that is Part 5).
Planning runs synchronously inside the HTTP request for now; Part 4 moves it to the background.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.context import CallContext, use_context
from app.agents import AgentInput, create_agent
from app.agents.planner import Plan, plannable_agent_types
from app.config import get_settings
from app.core.dag import PlanValidationError, execution_levels, to_mermaid, validate_plan
from app.core.enums import WorkflowStatus
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.events import EventType, record_event
from app.core.state_machine import transition_workflow
from app.llm.base import LLMClient
from app.models.task import Task
from app.models.workflow import Workflow
from app.schemas.plan import PlanConstraints
from app.schemas.task import TaskCreate
from app.schemas.workflow import WorkflowCreate
from app.services.workflow_service import (
    build_tasks,
    create_workflow,
    enforce_sensitive_policy,
    get_workflow,
    record_task_created_events,
)

logger = logging.getLogger(__name__)


class PlanningError(AppError):
    """The planner could not produce a valid plan within the repair budget."""

    status_code = 422
    code = "planning_failed"

    def __init__(self, errors: list[PlanValidationError], attempts: int) -> None:
        self.errors = errors
        messages = [e.message for e in errors]
        super().__init__(
            f"Planner could not produce a valid plan after {attempts} attempt(s): {'; '.join(messages)}",
            details=messages,
        )


@dataclass
class PlanResult:
    plan: Plan
    specs: list[TaskCreate]
    repair_attempts: int
    execution_levels: list[list[str]]
    mermaid: str
    forced_approvals: list[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


async def plan(
    llm: LLMClient,
    goal: str,
    constraints: PlanConstraints | None = None,
    context: dict[str, Any] | None = None,
) -> PlanResult:
    """Ask the planner for a plan and repair it until valid. Raises PlanningError or LLMError."""
    settings = get_settings()
    max_tasks = settings.max_plan_tasks
    if constraints and constraints.max_tasks:
        max_tasks = min(max_tasks, constraints.max_tasks)

    planner = create_agent("planner", llm)
    task_input: dict[str, Any] = {
        "context": context or {},
        "constraints": {**(constraints.model_dump(exclude_none=True) if constraints else {}),
                        "max_tasks": max_tasks},
    }
    totals = {"input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
    max_attempts = 1 + settings.planner_max_repair_attempts

    for attempt in range(1, max_attempts + 1):
        result = await planner.run(
            AgentInput(workflow_goal=goal, task_key="plan", task_name="Plan the workflow",
                       task_input=task_input)
        )
        for k in totals:
            totals[k] += getattr(result, k)
        candidate = Plan.model_validate(result.output)
        errors = validate_plan(candidate.tasks, plannable_agent_types(), max_tasks)
        logger.info("plan.attempt attempt=%d/%d tasks=%d errors=%d",
                    attempt, max_attempts, len(candidate.tasks), len(errors))
        if not errors:
            break
        task_input = {
            **task_input,
            "previous_plan": result.output,
            "validation_errors": [e.message for e in errors],
        }
    else:
        raise PlanningError(errors, attempts=max_attempts)

    forced = enforce_sensitive_policy(candidate.tasks)
    specs = [
        TaskCreate(
            key=t.key, name=t.name[:255], description=t.description, agent_type=t.agent_type,
            input=t.input, requires_approval=t.requires_approval, depends_on=t.depends_on,
        )
        for t in candidate.tasks
    ]
    return PlanResult(
        plan=candidate,
        specs=specs,
        repair_attempts=attempt - 1,
        execution_levels=execution_levels(specs),
        mermaid=to_mermaid(specs),
        forced_approvals=forced,
        model=result.model,
        **totals,
    )


async def plan_workflow(
    session: AsyncSession,
    workflow_id: uuid.UUID,
    llm: LLMClient,
    constraints: PlanConstraints | None = None,
) -> Workflow:
    """Plan an unplanned workflow and store the result. Re-raises planning/LLM errors after
    marking the workflow FAILED."""
    workflow = await _claim_for_planning(session, workflow_id, constraints)
    return await complete_planning(session, workflow, llm, constraints)


async def complete_planning(
    session: AsyncSession, workflow: Workflow, llm: LLMClient, constraints: PlanConstraints | None
) -> Workflow:
    """Second half of planning for a workflow already in PLANNING: LLM + validation + persist."""
    workflow_id = workflow.id  # still readable after a rollback expires `workflow`
    try:
        with use_context(CallContext(workflow_id)):  # planner calls count toward the workflow budget
            result = await plan(llm, workflow.goal, constraints, workflow.context)
    except Exception as exc:
        if await _still_planning(session, workflow):
            await _fail_planning(session, workflow, exc)
        raise

    # Re-check under a row lock: a cancel during the (long) LLM call wins, the plan is discarded.
    if not await _still_planning(session, workflow):
        return await get_workflow(session, workflow_id)

    try:
        tasks = build_tasks(workflow.id, result.specs)
        session.add_all(tasks)
        await session.flush()
        record_task_created_events(session, result.specs, tasks)
        # Reassign (not mutate) so SQLAlchemy detects the JSONB change.
        workflow.context = {**workflow.context, "plan_summary": result.plan.plan_summary}
        record_event(
            session,
            workflow_id=workflow.id,
            event_type=EventType.WORKFLOW_PLAN_CREATED,
            payload={
                "plan_summary": result.plan.plan_summary,
                "task_count": len(result.specs),
                "execution_levels": result.execution_levels,
                "repair_attempts": result.repair_attempts,
                "forced_approvals": result.forced_approvals,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
            },
        )
        await transition_workflow(session, workflow, WorkflowStatus.PENDING, reason="plan ready")
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.exception("plan.persist_failed workflow=%s", workflow.id)
        await session.refresh(workflow)  # rollback expired it; reload the committed PLANNING row
        await _fail_planning(session, workflow, exc)
        raise

    logger.info("plan.stored workflow=%s tasks=%d repairs=%d levels=%d", workflow.id,
                len(result.specs), result.repair_attempts, len(result.execution_levels))
    return await get_workflow(session, workflow.id)


async def _still_planning(session: AsyncSession, workflow: Workflow) -> bool:
    """Lock the workflow row and reload it. False (and rolled back) if it is no longer PLANNING."""
    await session.refresh(workflow, with_for_update=True)
    if workflow.status == WorkflowStatus.PLANNING:
        return True
    logger.warning("plan.discarded workflow=%s status=%s (changed during planning, e.g. cancelled)",
                   workflow.id, workflow.status)
    await session.rollback()  # expires `workflow`: callers must not read it afterwards
    return False


def stored_constraints(workflow: Workflow) -> PlanConstraints | None:
    """Constraints saved by POST /workflows run=true for the background planner."""
    raw = (workflow.context or {}).get("plan_constraints")
    return PlanConstraints.model_validate(raw) if raw else None


async def create_and_plan(session: AsyncSession, data: WorkflowCreate, llm: LLMClient) -> Workflow:
    """POST /workflows. run=true: store and return at once (the engine plans/starts it).
    auto_plan=true (sync, Part 3): plan in this request; a planning failure is not an HTTP
    error here: the workflow is returned with status FAILED and its error."""
    workflow = await create_workflow(session, data)
    if data.run or not data.auto_plan or data.tasks:
        return workflow
    try:
        return await plan_workflow(session, workflow.id, llm, data.constraints)
    except AppError:  # planning/LLM failure already recorded on the workflow
        return await get_workflow(session, workflow.id)


async def _claim_for_planning(
    session: AsyncSession, workflow_id: uuid.UUID, constraints: PlanConstraints | None
) -> Workflow:
    """Lock the row, check it may be planned, move it to PLANNING and COMMIT (visible to others)."""
    workflow = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if workflow is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    task_count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.workflow_id == workflow_id)
    )
    if task_count or workflow.status not in (WorkflowStatus.PENDING, WorkflowStatus.FAILED):
        await session.rollback()
        reason = f"it already has {task_count} tasks" if task_count else f"its status is {workflow.status}"
        raise ConflictError(
            f"Workflow {workflow_id} cannot be planned because {reason}. Only unplanned workflows "
            "that are PENDING, or FAILED during planning, can be planned."
        )
    await begin_planning(session, workflow, constraints)
    await session.commit()
    return workflow


async def begin_planning(
    session: AsyncSession, workflow: Workflow, constraints: PlanConstraints | None
) -> None:
    """Move a locked, plannable workflow to PLANNING and record planning_started (no commit)."""
    await transition_workflow(session, workflow, WorkflowStatus.PLANNING, reason="planning started")
    workflow.error = None
    record_event(
        session,
        workflow_id=workflow.id,
        event_type=EventType.WORKFLOW_PLANNING_STARTED,
        payload={"constraints": constraints.model_dump(exclude_none=True) if constraints else {}},
    )


async def _fail_planning(session: AsyncSession, workflow: Workflow, exc: Exception) -> None:
    message = getattr(exc, "message", None) or f"Planning failed: {type(exc).__name__}"
    errors = [e.message for e in exc.errors] if isinstance(exc, PlanningError) else [message]
    workflow.error = message
    await transition_workflow(session, workflow, WorkflowStatus.FAILED, reason="planning failed")
    record_event(
        session,
        workflow_id=workflow.id,
        event_type=EventType.WORKFLOW_PLANNING_FAILED,
        payload={"error_type": type(exc).__name__, "errors": errors},
    )
    await session.commit()
    logger.warning("plan.failed workflow=%s error_type=%s errors=%d", workflow.id, type(exc).__name__, len(errors))
