"""Authentication + roles (Part 11). One X-API-Key header: the ADMIN_API_KEY or a user's own key.

Backward compatible:
- no ADMIN_API_KEY and no users (development): everything open, as before;
- ADMIN_API_KEY and no users: reads open, controls/approvals need the admin key, as before;
- once a user exists: every scoped endpoint needs a key (tenant isolation needs to know the caller).
Keys are compared in constant time / looked up by sha256; neither the key nor its hash is ever logged.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, Header, Request
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.db.session import get_session
from app.governance.principal import SYSTEM, Principal, hash_key, set_current
from app.models.governance import Team, User


async def get_principal(
    x_api_key: str | None = Header(default=None, description="ADMIN_API_KEY or your own user key."),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[Principal]:
    admin = settings.admin_api_key
    if x_api_key:
        if admin is not None and hmac.compare_digest(x_api_key.encode(), admin.get_secret_value().encode()):
            p = Principal(kind="admin_key", role="admin")
        else:
            user = await session.scalar(select(User).where(User.api_key_hash == hash_key(x_api_key),
                                                           User.active.is_(True)))
            if user is None:
                raise UnauthorizedError("Missing or invalid X-API-Key header")
            team = await session.get(Team, user.team_id) if user.team_id else None
            p = Principal(kind="user", role=user.role, name=user.name, user_id=user.id, team_id=user.team_id,
                          team_name=team.name if team else None)
    elif await session.scalar(select(exists().where(User.id.is_not(None)))):
        p = Principal(kind="anonymous", role=None)
    elif admin is None:
        p = Principal(kind="open", role="admin")
    else:
        p = Principal(kind="open", role="viewer")  # admin key set, no users: reads stay open (Part 6 behaviour)
    set_current(p)
    try:
        yield p
    finally:
        set_current(SYSTEM)  # never leak a caller into code that runs later in the same task (tests, background)


def require_role(role: str) -> Callable[..., Awaitable[Principal]]:
    async def dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.at_least(role):
            return p
        if p.kind in ("anonymous", "open"):
            raise UnauthorizedError("Missing or invalid X-API-Key header")
        raise ForbiddenError(f"Your role '{p.role}' cannot do this (needs {role} or higher)")

    return dep


require_viewer = require_role("viewer")
require_operator = require_role("operator")
require_approver = require_role("approver")
require_admin_role = require_role("admin")


async def require_creator(p: Principal = Depends(get_principal)) -> Principal:
    """Create / plan / start: open in the legacy modes (as before), operator+ once users exist."""
    if p.kind == "open" or p.at_least("operator"):
        return p
    if p.kind == "anonymous":
        raise UnauthorizedError("Missing or invalid X-API-Key header")
    raise ForbiddenError(f"Your role '{p.role}' cannot do this (needs operator or higher)")


async def require_ops_view(p: Principal = Depends(get_principal)) -> Principal:
    """Cross-team operational views (metrics, engine status): open in the legacy modes, else a global admin."""
    if p.kind == "open" or (p.is_global and p.at_least("admin")):
        return p
    if p.kind == "anonymous":
        raise UnauthorizedError("Missing or invalid X-API-Key header")
    raise ForbiddenError("Only a global admin can see totals across all teams")


require_admin = require_operator  # Part 6 name: control endpoints (pause/resume/cancel/retry/budget/replan)


def scoped(param: str, checker: Callable[[AsyncSession, uuid.UUID], Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Router-level dependency: any caller must at least be a viewer; an object in the path must be in their team."""

    async def dep(request: Request, p: Principal = Depends(require_viewer),
                  session: AsyncSession = Depends(get_session)) -> None:
        raw = request.path_params.get(param)
        if raw is None:
            return
        try:
            oid = uuid.UUID(str(raw))
        except ValueError:
            return  # the route's own validation answers 422
        await checker(session, oid)

    return dep
