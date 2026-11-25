"""Plan preview: a dry run of the planner that stores nothing."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.llm import LLMClient, get_llm_client
from app.schemas.common import ErrorResponse
from app.schemas.plan import PlanPreviewRequest, PlanPreviewResponse
from app.services import planning_service

router = APIRouter(prefix="/plans", tags=["plans"])


@router.post(
    "/preview",
    response_model=PlanPreviewResponse,
    responses={
        422: {"model": ErrorResponse, "description": "Planner could not produce a valid plan"},
        502: {"model": ErrorResponse, "description": "LLM rejected the request or returned bad output"},
        503: {"model": ErrorResponse, "description": "LLM temporarily unavailable"},
    },
)
async def preview_plan(
    body: PlanPreviewRequest, llm: LLMClient = Depends(get_llm_client)
) -> PlanPreviewResponse:
    result = await planning_service.plan(llm, body.goal, body.constraints, body.context)
    return PlanPreviewResponse(
        plan=result.plan,
        execution_levels=result.execution_levels,
        mermaid=result.mermaid,
        repair_attempts=result.repair_attempts,
        forced_approvals=result.forced_approvals,
    )
