"""Who can approve (Part 12 Step 0): never let an approval wait silently for people who don't exist."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.governance.principal import ROLE_RANK
from app.models.governance import Team, User


async def users_exist(session: AsyncSession) -> bool:
    return bool(await session.scalar(select(exists().where(User.active.is_(True)))))


async def eligible_approvers(session: AsyncSession, team_id: uuid.UUID | None, role: str | None,
                             exclude: str | None = None) -> list[str]:
    """Active users of the team (or global users, who see every team) with at least `role` (default approver)."""
    need = ROLE_RANK[role or "approver"]
    stmt = select(User).where(User.active.is_(True),
                              or_(User.team_id.is_(None), User.team_id == team_id) if team_id else User.team_id.is_(None))
    return sorted(u.name for u in await session.scalars(stmt) if ROLE_RANK[u.role] >= need and u.name != exclude)


async def admins_for(session: AsyncSession, team_id: uuid.UUID | None) -> list[str]:
    return await eligible_approvers(session, team_id, "admin")


async def policy_warnings(session: AsyncSession) -> list[dict[str, Any]]:
    """Per rule and team: rules needing more distinct approvers than the team has (checked with current membership)."""
    from app.governance.policy import get_policies

    if not await users_exist(session):
        return []
    teams = {t.id: t.name for t in await session.scalars(select(Team))}
    out = []
    for rule in get_policies().rules:
        if not (rule.approvals or rule.approver_role):
            continue
        need = rule.approvals or 1
        for tid, name in teams.items():
            if rule.when.team and rule.when.team != name:
                continue
            # four eyes: with 2+ approvers the creator can't vote, so one more person must exist
            people = await eligible_approvers(session, tid, rule.approver_role)
            usable = len(people) - (1 if need >= 2 and people else 0)
            if usable < need:
                out.append({"rule": rule.name, "team": name, "needs": need, "eligible": len(people),
                            "message": f"rule {rule.name}: needs {need} approvers, team {name} has only "
                                       f"{max(usable, 0)} eligible (besides the creator)"})
    return out
