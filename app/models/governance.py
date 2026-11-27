"""Teams and users (Part 11). A user belongs to one team and has one role; their API key is stored hashed."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

ROLES = ("viewer", "operator", "approver", "admin")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4,
                                          server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4,
                                          server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(String(64), unique=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"))
    role: Mapped[str] = mapped_column(String(16))  # viewer | operator | approver | admin
    # sha256 of the key; the key itself is shown once at creation and never stored or logged.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # argon2id; None = API key only (Part 12 Step 0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class Session(Base):
    """A browser login (Part 12 Step 0). The cookie holds a random token; only its sha256 is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4,
                                          server_default=text("gen_random_uuid()"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))  # the X-CSRF-Token header must match (synchronizer token)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    ip: Mapped[str | None] = mapped_column(String(64))


class LoginAttempt(Base):
    """Failed logins per key ("name:<name>" or "ip:<addr>"), in the DB so a lockout survives restarts."""

    __tablename__ = "login_attempts"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    failures: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    first_failure_at: Mapped[datetime | None] = mapped_column()
    locked_until: Mapped[datetime | None] = mapped_column()
