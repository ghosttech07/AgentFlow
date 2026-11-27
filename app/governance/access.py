"""Tenant isolation (Part 11): a user with a team only sees that team's workflows, approvals and memories.

Another team's object answers 404 (not 403), so its existence is not revealed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.governance.principal import Principal, current
from app.models.approval import Approval
from app.models.workflow import Workflow


def workflow_filters(p: Principal | None = None) -> list[Any]:
    p = p or current()
    return [] if p.is_global else [Workflow.team_id == p.team_id]


async def check_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> None:
    p = current()
    if p.is_global:
        return
    team = await session.scalar(select(Workflow.team_id).where(Workflow.id == workflow_id))
    if team != p.team_id:
        raise NotFoundError(f"Workflow {workflow_id} not found")


async def check_approval(session: AsyncSession, approval_id: uuid.UUID) -> None:
    p = current()
    if p.is_global:
        return
    team = await session.scalar(select(Workflow.team_id).join(Approval, Approval.workflow_id == Workflow.id)
                                .where(Approval.id == approval_id))
    if team != p.team_id:
        raise NotFoundError(f"Approval {approval_id} not found")


def team_prefix(team_id: uuid.UUID | None) -> str:
    return f"team-{team_id}:" if team_id else ""


def memory_namespace(requested: str, p: Principal | None = None) -> str:
    """A team user's memory lives under the team's prefix; they can never name another team's namespace."""
    p = p or current()
    prefix = "" if p.is_global else team_prefix(p.team_id)
    return requested if requested.startswith(prefix) else prefix + requested


def check_memory_namespace(namespace: str) -> None:
    p = current()
    if not p.is_global and not namespace.startswith(team_prefix(p.team_id)):
        raise NotFoundError("Memory not found")
