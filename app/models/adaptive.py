"""Part 10: PlanVersion (every plan change, auditable) and Memory (facts/lessons reused across workflows)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Computed, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class PlanVersion(Base):
    """Version 1 = the original plan; later versions = applied (or proposed/rejected) plan patches."""

    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="applied", server_default="applied")  # proposed|applied|rejected|failed
    trigger: Mapped[str] = mapped_column(String(32))  # initial | task_failed | replan_if | human | human_patch
    reason: Mapped[str | None] = mapped_column(Text)
    patch: Mapped[dict[str, Any]] = mapped_column()
    created_by: Mapped[str] = mapped_column(String(255))
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approval_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column()


class Memory(Base):
    """A fact, lesson or artifact summary stored by CODE from a finished task, reused by later workflows."""

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_memories_namespace_kind", "namespace", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    namespace: Mapped[str] = mapped_column(String(64), default="default", server_default="default")
    kind: Mapped[str] = mapped_column(String(32))  # fact | lesson | artifact_summary
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column()
    sources: Mapped[list[Any] | None] = mapped_column(JSONB)
    source_workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflows.id", ondelete="SET NULL"))
    source_task_key: Mapped[str | None] = mapped_column(String(64))
    tainted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    private: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    confidence: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column()
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    deleted_at: Mapped[datetime | None] = mapped_column()
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('english', content)", persisted=True),
                                               nullable=True)
