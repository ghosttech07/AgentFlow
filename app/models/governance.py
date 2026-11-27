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
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
