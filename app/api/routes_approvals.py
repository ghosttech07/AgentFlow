"""Approval endpoints (Part 6)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_approver, scoped
from app.db.session import get_session
from app.engine import Engine
from app.engine.deps import get_engine
from app.governance import access
from app.governance.principal import Principal
from app.schemas.common import ErrorResponse
from app.schemas.control import ApprovalRead, ApproveRequest, CheckRequest, RejectRequest
from app.services import approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"],
                   dependencies=[Depends(scoped("approval_id", access.check_approval))])

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


@router.post("/{approval_id}/approve", response_model=ApprovalRead, responses=DECISION)
async def approve(
    approval_id: uuid.UUID, body: ApproveRequest, session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine), p: Principal = Depends(require_approver),
) -> ApprovalRead:
    """Approve (optionally correcting the task input). The task becomes READY."""
    await approval_service.check_approve_fields(session, approval_id, set(body.model_fields_set))
    approval = await approval_service.approve(session, approval_id, actor=p.actor(body.actor), comment=body.comment,
                                              input_overrides=body.input_overrides,
                                              arguments_overrides=body.arguments_overrides,
                                              allow_recipient_override=body.allow_recipient_override,
                                              confirm_duplicate=body.confirm_duplicate,
                                              extra_iterations=body.extra_iterations)
    if engine:
        engine.wake()
    return ApprovalRead.model_validate(approval)


@router.post("/{approval_id}/check", responses={404: {"model": ErrorResponse}})
async def check_approval(approval_id: uuid.UUID, body: CheckRequest,
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Would approving with these arguments repeat a call that already succeeded? (The card warns while editing.)"""
    approval = await approval_service.get_approval(session, approval_id)
    key, sent_at = await approval_service.duplicate_of(session, approval, body.arguments_overrides)
    return {"idempotency_key": key or None, "already_sent_at": sent_at.isoformat() if sent_at else None}


@router.post("/{approval_id}/reject", response_model=ApprovalRead, responses=DECISION)
async def reject(
    approval_id: uuid.UUID, body: RejectRequest, session: AsyncSession = Depends(get_session),
    engine: Engine | None = Depends(get_engine), p: Principal = Depends(require_approver),
) -> ApprovalRead:
    """fail_workflow: task FAILED (rejected) and workflow FAILED. skip_task: task and all its
    descendants SKIPPED; other branches continue."""
    on_reject = body.on_reject or await approval_service.default_on_reject(session, approval_id)
    approval = await approval_service.reject(session, approval_id, actor=p.actor(body.actor), reason=body.reason,
                                             on_reject=on_reject)
    if engine:
        engine.wake()
    return ApprovalRead.model_validate(approval)
