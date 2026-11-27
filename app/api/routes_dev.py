"""Dev-only chaos injection (404 when APP_ENV=production). Rules are persisted in the DB."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_creator
from app.config import Settings, get_settings
from app.governance import access
from app.core.errors import NotFoundError
from app.db.session import get_session
from app.models.attempt import ChaosRule
from app.models.workflow import Workflow
from app.schemas.attempt import ChaosCreate, ChaosRead

router = APIRouter(prefix="/dev/chaos", tags=["dev"], dependencies=[Depends(require_creator)])  # operator+ with users


def _dev_only(settings: Settings = Depends(get_settings)) -> None:
    if settings.app_env == "production":
        raise NotFoundError("Chaos endpoints are disabled in production")


@router.post("", response_model=ChaosRead, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_dev_only)])
async def add_rule(body: ChaosCreate, session: AsyncSession = Depends(get_session)) -> ChaosRule:
    if await session.get(Workflow, body.workflow_id) is None:
        raise NotFoundError(f"Workflow {body.workflow_id} not found")
    await access.check_workflow(session, body.workflow_id)  # never on another team's workflow
    rule = ChaosRule(workflow_id=body.workflow_id, task_key=body.task_key, mode=body.mode,
                     remaining=body.times)
    session.add(rule)
    await session.commit()
    return rule


@router.get("", response_model=list[ChaosRead], dependencies=[Depends(_dev_only)])
async def list_rules(
    workflow_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)
) -> list[ChaosRule]:
    stmt = select(ChaosRule).order_by(ChaosRule.created_at)
    if team := access.workflow_filters():
        stmt = stmt.join(Workflow, Workflow.id == ChaosRule.workflow_id).where(*team)
    if workflow_id:
        stmt = stmt.where(ChaosRule.workflow_id == workflow_id)
    return list((await session.scalars(stmt)).all())


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_dev_only)])
async def delete_rule(rule_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> None:
    rule = await session.get(ChaosRule, rule_id)
    if rule is None:
        raise NotFoundError(f"Chaos rule {rule_id} not found")
    await access.check_workflow(session, rule.workflow_id)
    await session.delete(rule)
    await session.commit()
