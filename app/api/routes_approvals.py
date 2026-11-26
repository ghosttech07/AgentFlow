"""Approval endpoints (Part 6)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.db.session import get_session
from app.engine import Engine
from app.engine.deps import get_engine
from app.schemas.common import ErrorResponse
from app.schemas.control import ApprovalRead, ApproveRequest, RejectRequest
from app.services import approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])

DECISION = {401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
            409: {"model": ErrorResponse, "description": "Not pending, or expired"}}


@router.get("", response_model=list[ApprovalRead])
async def list_approvals(
    status: Literal["pending", "approved", "rejected", "expired", "cancelled"] | None = Query(default=None),
    workflow_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalRead]:
    """Oldest first."""
    rows = await approval_service.list_approvals(session, status, workflow_id)
    return [ApprovalRead.model_validate(r) for r in rows]


@router.get("/{approval_id}", response_model=ApprovalRead, responses={404: {"model": ErrorResponse}})
async def get_approval(approval_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ApprovalRead:
    return ApprovalRead.model_validate(await approval_service.get_approval(session, approval_id))


@router.post("/{approval_id}/approve", response_model=ApprovalRead, responses=DECISION,
             dependencies=[Depends(require_admin)])
async def approve(
    approval_id: uuid.UUID, body: ApproveRequest, session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> ApprovalRead:
    """Approve (optionally correcting the task input). The task becomes READY."""
    approval = await approval_service.approve(session, approval_id, actor=body.actor, comment=body.comment,
                                              input_overrides=body.input_overrides,
                                              arguments_overrides=body.arguments_overrides,
                                              allow_recipient_override=body.allow_recipient_override)
    if engine:
        engine.wake()
    return ApprovalRead.model_validate(approval)


@router.post("/{approval_id}/reject", response_model=ApprovalRead, responses=DECISION,
             dependencies=[Depends(require_admin)])
async def reject(
    approval_id: uuid.UUID, body: RejectRequest, session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine),
) -> ApprovalRead:
    """fail_workflow: task FAILED (rejected) and workflow FAILED. skip_task: task and all its
    descendants SKIPPED; other branches continue."""
    on_reject = body.on_reject or await approval_service.default_on_reject(session, approval_id)
    approval = await approval_service.reject(session, approval_id, actor=body.actor, reason=body.reason,
                                             on_reject=on_reject)
    if engine:
        engine.wake()
    return ApprovalRead.model_validate(approval)
