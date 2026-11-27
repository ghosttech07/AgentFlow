"""Privacy endpoints (Part 12): retention settings, erasure requests (two approvers), PII summary, reveal (audited)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_permission
from app.audit import chain as audit
from app.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.session import get_session
from app.governance import access
from app.governance.principal import Principal
from app.models.privacy import ErasureRequest, RetentionSetting
from app.privacy import erasure, retention

router = APIRouter(prefix="/privacy", tags=["privacy"])


class RetentionBody(BaseModel):
    days: dict[str, int | None] = Field(description="days per category (stored_content, outputs, memories, "
                                                    "tool_results, files); null = keep forever")


class ErasureBody(BaseModel):
    subjects: list[str] = Field(min_length=1, max_length=20, description="e.g. an email, phone or Aadhaar number")
    reason: str = Field(min_length=3, max_length=1000)
    actor: str | None = None


class VoteBody(BaseModel):
    actor: str | None = None


async def _setting(session: AsyncSession, p: Principal) -> RetentionSetting | None:
    cond = RetentionSetting.team_id == p.team_id if p.team_id else RetentionSetting.team_id.is_(None)
    return await session.scalar(select(RetentionSetting).where(cond))


@router.get("/retention")
async def get_retention(p: Principal = Depends(require_permission("privacy.retention")),
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _setting(session, p)
    return {"team": p.team_name, "days": row.days if row else {}, "categories": list(retention.CATEGORIES)}


@router.put("/retention")
async def put_retention(body: RetentionBody, p: Principal = Depends(require_permission("privacy.retention")),
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    bad = set(body.days) - set(retention.CATEGORIES)
    if bad:
        raise ValidationFailedError(f"unknown categories: {', '.join(sorted(bad))}")
    row = await _setting(session, p)
    if row is None:
        row = RetentionSetting(team_id=p.team_id, days={})
        session.add(row)
    row.days = {k: v for k, v in {**(row.days or {}), **body.days}.items() if v is not None}
    row.updated_by = p.actor(None)
    audit.enqueue(session, action="retention.updated", team_id=p.team_id, object_type="retention",
                  payload={"days": row.days})
    await session.commit()
    return {"team": p.team_name, "days": row.days}


@router.post("/retention/apply")
async def apply_retention(p: Principal = Depends(require_permission("privacy.retention")),
                          settings: Settings = Depends(get_settings),
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _setting(session, p)
    return {"deleted": await retention.apply_team(session, row, settings) if row else {}}


@router.post("/erasure", status_code=201)
async def request_erasure(body: ErasureBody, p: Principal = Depends(require_permission("privacy.erase")),
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Needs two OTHER approvers (privacy.erase) before anything is deleted."""
    req = await erasure.create(session, body.subjects, body.reason, p.actor(body.actor), p.team_id)
    return erasure.to_dict(req)


@router.get("/erasure")
async def list_erasures(p: Principal = Depends(require_permission("privacy.erase")),
                        session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(ErasureRequest).order_by(ErasureRequest.created_at.desc()).limit(100)
    if not p.is_global:
        stmt = stmt.where(ErasureRequest.team_id == p.team_id)
    return [erasure.to_dict(r) for r in await session.scalars(stmt)]


@router.get("/erasure/{request_id}")
async def get_erasure(request_id: uuid.UUID, p: Principal = Depends(require_permission("privacy.erase")),
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    req = await session.get(ErasureRequest, request_id)
    if req is None:
        raise NotFoundError("Not found")
    access.check_team(req.team_id)
    return erasure.to_dict(req)


@router.post("/erasure/{request_id}/approve")
async def approve_erasure(request_id: uuid.UUID, body: VoteBody | None = None,
                          p: Principal = Depends(require_permission("privacy.erase")),
                          settings: Settings = Depends(get_settings),
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    req = await erasure.vote(session, request_id, p.actor((body or VoteBody()).actor), True, settings)
    return erasure.to_dict(req)


@router.post("/erasure/{request_id}/reject")
async def reject_erasure(request_id: uuid.UUID, body: VoteBody | None = None,
                         p: Principal = Depends(require_permission("privacy.erase")),
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return erasure.to_dict(await erasure.vote(session, request_id, p.actor((body or VoteBody()).actor), False))


@router.get("/workflows/{workflow_id}/pii")
async def pii(workflow_id: uuid.UUID, p: Principal = Depends(require_permission("audit.view")),
              session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """What was protected per task (types and actions only, never values)."""
    from app.services import trust_service

    await access.check_workflow(session, workflow_id)
    return {"tasks": await trust_service.pii_summary(session, workflow_id)}


@router.post("/workflows/{workflow_id}/reveal")
async def reveal(workflow_id: uuid.UUID, p: Principal = Depends(require_permission("privacy.reveal")),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Task outputs with the tokenized values put back (privacy.reveal). Every reveal is audited."""
    from app.models.task import Task
    from app.privacy.crypto import ShreddedError
    from app.privacy.guard import rehydrate

    await access.check_workflow(session, workflow_id)
    tasks = list(await session.scalars(select(Task).where(Task.workflow_id == workflow_id).order_by(Task.created_at)))
    try:
        outputs = {t.key: await rehydrate(session, workflow_id, t.output) for t in tasks}
    except ShreddedError:
        outputs = {"_shredded": "the data key of this workflow was deleted; tokens cannot be revealed"}
    await audit.append_now(session, action="privacy.reveal", workflow_id=workflow_id, object_type="workflow",
                           object_id=workflow_id, payload={"tasks": len(tasks)})
    await session.commit()
    return {"outputs": outputs}
