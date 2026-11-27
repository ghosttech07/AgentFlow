"""Who is calling (Part 11). Set once per request by `app.api.auth.get_principal`; services read `current()`.

Kinds:
- open      : no ADMIN_API_KEY and no users yet (development): full access, the body's `actor` names the caller.
- admin_key : the ADMIN_API_KEY (bootstrap / break-glass): global admin, sees every team.
- user      : a user's own key: their role, their team only, `actor` = their name (the body cannot change it).
- anonymous : no key while keys are required (only reads that are still public).
- system    : the engine (no request).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

ROLE_RANK = {"viewer": 0, "operator": 1, "approver": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    kind: str
    role: str | None
    name: str | None = None
    user_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    team_name: str | None = None
    auth_method: str | None = None  # api_key | session | admin_key | none
    ip: str | None = None

    def at_least(self, role: str) -> bool:
        return self.role is not None and ROLE_RANK[self.role] >= ROLE_RANK[role]

    @property
    def is_global(self) -> bool:
        """Sees every team: the open dev mode, the admin key, the engine, or a user without a team."""
        return self.kind in ("open", "admin_key", "system") or (self.kind == "user" and self.team_id is None)

    def actor(self, requested: str | None) -> str:
        """A real user is always recorded as themselves; other callers may name the actor (backward compatible)."""
        if self.kind == "user" and self.name:
            return self.name
        return requested or "admin"


SYSTEM = Principal(kind="system", role="admin", name="engine")
_current: ContextVar[Principal] = ContextVar("principal", default=SYSTEM)


def current() -> Principal:
    return _current.get()


def set_current(p: Principal) -> None:
    _current.set(p)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "af_" + secrets.token_urlsafe(32)
