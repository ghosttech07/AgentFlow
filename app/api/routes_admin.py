"""Users, teams and keys (Part 11). Bootstrap with the ADMIN_API_KEY; a team admin manages only their own team."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_principal, require_admin_role
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.db.session import get_session
from app.governance.policy import SAFETY_FLOOR, get_policies
from app.governance.principal import Principal, hash_key, new_key
from app.models.governance import Team, User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])
NAME = r"^[a-z][a-z0-9_.-]{0,63}$"


class TeamCreate(BaseModel):
    name: str = Field(pattern=NAME)


class UserCreate(BaseModel):
    name: str = Field(pattern=NAME)
    role: Literal["viewer", "operator", "approver", "admin"]
    team: str | None = Field(default=None, description="Team name; omitted = global (only the admin key may).")


def _user(u: User, teams: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {"id": str(u.id), "name": u.name, "role": u.role, "team": teams.get(u.team_id) if u.team_id else None,
            "active": u.active, "created_at": u.created_at.isoformat() if u.created_at else None}


async def _teams(session: AsyncSession) -> dict[uuid.UUID, str]:
    return {t.id: t.name for t in await session.scalars(select(Team))}


@router.get("/me")
async def me(p: Principal = Depends(get_principal)) -> dict[str, Any]:
    """Who the X-API-Key belongs to (the dashboard shows this)."""
    return {"kind": p.kind, "name": p.name, "role": p.role, "team": p.team_name, "sees_all_teams": p.is_global}


@router.get("/admin/policies")
async def policies(_: Principal = Depends(require_admin_role)) -> dict[str, Any]:
    return {"rules": [r.model_dump(exclude_none=True) for r in get_policies().rules], "safety_floor": list(SAFETY_FLOOR)}


@router.post("/admin/teams", status_code=201)
async def create_team(body: TeamCreate, p: Principal = Depends(require_admin_role),
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not p.is_global:
        raise ForbiddenError("Only a global admin can create teams")
    team = Team(name=body.name)
    session.add(team)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"Team '{body.name}' already exists") from exc
    return {"id": str(team.id), "name": team.name}


@router.get("/admin/teams")
async def list_teams(_: Principal = Depends(require_admin_role),
                     session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    return [{"id": str(i), "name": n} for i, n in (await _teams(session)).items()]


@router.post("/admin/users", status_code=201)
async def create_user(body: UserCreate, p: Principal = Depends(require_admin_role),
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Returns the user's API key ONCE; only its sha256 is stored."""
    team_id = None
    if body.team:
        team_id = await session.scalar(select(Team.id).where(Team.name == body.team))
        if team_id is None:
            raise NotFoundError(f"Team '{body.team}' not found")
    if not p.is_global and team_id != p.team_id:
        raise ForbiddenError("A team admin can only add users to their own team")
    key = new_key()
    user = User(name=body.name, role=body.role, team_id=team_id, api_key_hash=hash_key(key))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"User '{body.name}' already exists") from exc
    logger.info("user.created name=%s role=%s team=%s by=%s", user.name, user.role, body.team, p.name or p.kind)
    return {**_user(user, {team_id: body.team} if team_id else {}), "api_key": key}


@router.get("/admin/users")
async def list_users(p: Principal = Depends(require_admin_role),
                     session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(User).order_by(User.name)
    if not p.is_global:
        stmt = stmt.where(User.team_id == p.team_id)
    teams = await _teams(session)
    return [_user(u, teams) for u in await session.scalars(stmt)]


async def _managed(session: AsyncSession, p: Principal, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or (not p.is_global and user.team_id != p.team_id):
        raise NotFoundError(f"User {user_id} not found")
    return user


@router.post("/admin/users/{user_id}/disable")
async def disable_user(user_id: uuid.UUID, p: Principal = Depends(require_admin_role),
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    user = await _managed(session, p, user_id)
    user.active = False
    await session.commit()
    return _user(user, await _teams(session))


@router.post("/admin/users/{user_id}/rotate-key")
async def rotate_key(user_id: uuid.UUID, p: Principal = Depends(require_admin_role),
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """The old key stops working at once; the new one is shown once."""
    user = await _managed(session, p, user_id)
    key = new_key()
    user.api_key_hash = hash_key(key)
    await session.commit()
    return {**_user(user, await _teams(session)), "api_key": key}
