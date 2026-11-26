"""Workflow endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.enums import WorkflowStatus
from app.db.session import get_session
from app.engine import Engine
from app.engine.deps import get_engine
from app.llm import LLMClient, get_llm_client
from app.schemas.attempt import AttemptRead
from app.schemas.common import ErrorResponse
from app.schemas.control import CancelRequest, PauseRequest, ResumeRequest
from app.schemas.event import EventRead
from app.schemas.plan import PlanRequest, WorkflowGraph
from app.schemas.task import TaskRead
from app.schemas.workflow import BudgetUpdate, WorkflowCreate, WorkflowDetail, WorkflowList, WorkflowRead
from app.services import budget_service, control_service, planning_service, timeline_service, workflow_service

router = APIRouter(prefix="/workflows", tags=["workflows"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Workflow not found"}}
CONTROL = {**NOT_FOUND, 401: {"model": ErrorResponse, "description": "Missing or invalid X-API-Key"}}


@router.post(
    "",
    response_model=WorkflowDetail,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ErrorResponse, "description": "Invalid request or task graph"}},
)
async def create_workflow(
    body: WorkflowCreate,
    response: Response,
    session: AsyncSession = Depends(get_session),
    llm: LLMClient = Depends(get_llm_client),
    engine: Engine | None = Depends(get_engine),
) -> WorkflowDetail:
    """Create a workflow.

    - run=true: 202 immediately; the engine plans it in the background (if no tasks) and runs it.
    - auto_plan=true (no tasks): planned synchronously in this request (Part 3 behaviour); a
      planning failure still returns 201 with status FAILED and the error.
    """
    workflow = await planning_service.create_and_plan(session, body, llm)
    if body.run:
        response.status_code = status.HTTP_202_ACCEPTED
        if engine:
            engine.wake()
    return WorkflowDetail.from_model(workflow)


@router.post(
    "/{workflow_id}/start",
    response_model=WorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "No tasks, already running, or terminal"}},
)
async def start_workflow(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> WorkflowDetail:
    """PENDING workflow with tasks -> RUNNING. The engine runs root tasks on its next tick."""
    workflow = await workflow_service.start_workflow(session, workflow_id)
    if engine:
        engine.wake()
    return WorkflowDetail.from_model(workflow)


@router.get(
    "/{workflow_id}/result",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Workflow not COMPLETED yet"}},
)
async def get_workflow_result(
    workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """{"final_outputs": {leaf_task_key: output}, "summary": plan_summary} once COMPLETED."""
    return await workflow_service.get_result(session, workflow_id)


@router.post(
    "/{workflow_id}/retry",
    response_model=WorkflowDetail,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**CONTROL, 409: {"model": ErrorResponse, "description": "Workflow is not FAILED"}},
    dependencies=[Depends(require_admin)],
)
async def retry_workflow(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> WorkflowDetail:
    """Retry only the failed part: FAILED tasks -> PENDING (attempts reset), SUCCEEDED kept.
    A workflow that failed during planning is re-planned."""
    workflow = await workflow_service.retry_workflow(session, workflow_id)
    if engine:
        engine.wake()
    return WorkflowDetail.from_model(workflow)


@router.post("/{workflow_id}/pause", response_model=WorkflowDetail, dependencies=[Depends(require_admin)],
             responses={**CONTROL, 409: {"model": ErrorResponse, "description": "Not RUNNING / AWAITING_APPROVAL"}})
async def pause_workflow(
    workflow_id: uuid.UUID,
    body: PauseRequest | None = None,
    session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> WorkflowDetail:
    """Pause (graceful: running tasks finish; immediate: they are interrupted). Idempotent."""
    body = body or PauseRequest()
    workflow, interrupted = await control_service.pause(session, workflow_id, actor=body.actor,
                                                        reason=body.reason, mode=body.mode)
    if engine:
        engine.interrupt(interrupted)
    return WorkflowDetail.from_model(workflow)


@router.post("/{workflow_id}/resume", response_model=WorkflowDetail, dependencies=[Depends(require_admin)],
             responses={**CONTROL, 409: {"model": ErrorResponse, "description": "Not PAUSED"}})
async def resume_workflow(
    workflow_id: uuid.UUID,
    body: ResumeRequest | None = None,
    session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> WorkflowDetail:
    """PAUSED -> RUNNING (or AWAITING_APPROVAL if only approvals block it). Completed work is kept."""
    workflow = await control_service.resume(session, workflow_id, actor=(body or ResumeRequest()).actor)
    if engine:
        engine.wake()
    return WorkflowDetail.from_model(workflow)


@router.post("/{workflow_id}/cancel", response_model=WorkflowDetail, dependencies=[Depends(require_admin)],
             responses={**CONTROL, 409: {"model": ErrorResponse, "description": "Already COMPLETED"}})
async def cancel_workflow(
    workflow_id: uuid.UUID,
    body: CancelRequest | None = None,
    session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> WorkflowDetail:
    """Cancel all unfinished tasks and pending approvals; running results are discarded. Idempotent."""
    body = body or CancelRequest()
    workflow, running = await control_service.cancel(session, workflow_id, actor=body.actor, reason=body.reason)
    if engine:
        engine.interrupt(running)
    return WorkflowDetail.from_model(workflow)


@router.get("/{workflow_id}/tasks/{key}/attempts", response_model=list[AttemptRead], responses=NOT_FOUND)
async def list_task_attempts(
    workflow_id: uuid.UUID, key: str, session: AsyncSession = Depends(get_session)
) -> list[AttemptRead]:
    """Attempt history, oldest first (e.g. 1 = worker_lost, 2 = succeeded)."""
    rows = await workflow_service.list_attempts(session, workflow_id, key)
    return [AttemptRead.model_validate(r) for r in rows]


@router.get("/{workflow_id}/plan-versions", responses=NOT_FOUND)
async def list_plan_versions(workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    """Every version of the plan (1 = the original; later = re-plan patches, incl. proposed/rejected ones)."""
    from app.services.graph_service import list_plan_versions as _list

    await workflow_service.get_workflow(session, workflow_id)
    return [{"version": v.version, "status": v.status, "trigger": v.trigger, "reason": v.reason, "patch": v.patch,
             "created_by": v.created_by, "approved_by": v.approved_by, "created_at": v.created_at.isoformat(),
             "applied_at": v.applied_at.isoformat() if v.applied_at else None} for v in await _list(session, workflow_id)]


@router.get("/{workflow_id}/tasks/{key}/tool-calls", responses=NOT_FOUND)
async def list_task_tool_calls(
    workflow_id: uuid.UUID, key: str, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    """The task's tool journal (Part 9), oldest first: every proposed tool call with its arguments (secrets
    redacted), status, result preview, duration, and whether it was reused or made with untrusted context."""
    return await workflow_service.list_tool_calls(session, workflow_id, key)


@router.get("/{workflow_id}/tasks/{key}", response_model=TaskRead, responses=NOT_FOUND)
async def get_workflow_task(
    workflow_id: uuid.UUID, key: str, session: AsyncSession = Depends(get_session)
) -> TaskRead:
    """Full task detail: output, metrics, error, error_type, attempt_count, lease."""
    return TaskRead.from_model(await workflow_service.get_task_by_key(session, workflow_id, key))


@router.get("", response_model=WorkflowList)
async def list_workflows(
    status_filter: WorkflowStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> WorkflowList:
    items, total = await workflow_service.list_workflows(
        session, status=status_filter, limit=limit, offset=offset
    )
    return WorkflowList(
        items=[WorkflowRead.model_validate(w) for w in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{workflow_id}", response_model=WorkflowDetail, responses=NOT_FOUND)
async def get_workflow(
    workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> WorkflowDetail:
    workflow = await workflow_service.get_workflow(session, workflow_id)
    return await workflow_service.detail_with_usage(session, workflow)


@router.patch("/{workflow_id}/budget", response_model=WorkflowDetail, dependencies=[Depends(require_admin)],
              responses=CONTROL)
async def update_workflow_budget(
    workflow_id: uuid.UUID, body: BudgetUpdate, session: AsyncSession = Depends(get_session),
) -> WorkflowDetail:
    """Raise or adjust budget limits (merged onto the current ones; null = unlimited). A workflow
    paused for its budget stays PAUSED: resume it with POST /workflows/{id}/resume."""
    await budget_service.update_budget(session, workflow_id, actor=body.actor, budget=body.budget.stored())
    workflow = await workflow_service.get_workflow(session, workflow_id)
    return await workflow_service.detail_with_usage(session, workflow)


@router.post(
    "/{workflow_id}/plan",
    response_model=WorkflowDetail,
    responses={
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Already has tasks, or not PENDING/FAILED"},
        422: {"model": ErrorResponse, "description": "Planner could not produce a valid plan"},
        502: {"model": ErrorResponse, "description": "LLM rejected the request or returned bad output"},
        503: {"model": ErrorResponse, "description": "LLM temporarily unavailable"},
    },
)
async def plan_workflow(
    workflow_id: uuid.UUID,
    body: PlanRequest | None = None,
    session: AsyncSession = Depends(get_session),
    llm: LLMClient = Depends(get_llm_client),
) -> WorkflowDetail:
    """Plan an unplanned workflow. Runs synchronously (~15-60 s); Part 4 moves it to the background.
    On failure the workflow is marked FAILED (re-plannable) and the error is returned."""
    constraints = body.constraints if body else None
    workflow = await planning_service.plan_workflow(session, workflow_id, llm, constraints)
    return WorkflowDetail.from_model(workflow)


@router.get("/{workflow_id}/graph", response_model=WorkflowGraph, responses=NOT_FOUND)
async def get_workflow_graph(
    workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    return await workflow_service.get_workflow_graph(session, workflow_id)


@router.get("/{workflow_id}/tasks", response_model=list[TaskRead], responses=NOT_FOUND)
async def list_workflow_tasks(
    workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[TaskRead]:
    tasks = await workflow_service.list_tasks(session, workflow_id)
    return [TaskRead.from_model(t) for t in tasks]


@router.get("/{workflow_id}/timeline", responses=NOT_FOUND)
async def get_workflow_timeline(
    workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Readable timeline: summary (attempts, retries, restarts, tokens), per-task attempt spans
    and one described entry per event, with offsets in seconds since creation."""
    return await timeline_service.get_timeline(session, workflow_id)


@router.get("/{workflow_id}/events", response_model=list[EventRead], responses=NOT_FOUND)
async def list_workflow_events(
    workflow_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[EventRead]:
    events = await workflow_service.list_events(session, workflow_id)
    return [EventRead.model_validate(e) for e in events]
