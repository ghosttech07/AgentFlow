"""Ask: one Quick Ask question and its answer (or error), kept for the history view."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class Ask(Base):
    __tablename__ = "asks"
    __table_args__ = (Index("ix_asks_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # answered | failed
    result: Mapped[dict[str, Any] | None] = mapped_column()  # the full /ask response
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Part 11: whose question (history is per team; None = global / before teams)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
