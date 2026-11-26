"""Approval (human checkpoint for one task run) and Publication (publisher side effects, Part 6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.workflow import Workflow

APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "cancelled")


class Approval(Base):
    """A human decision about a requires_approval task. At most one PENDING row per task."""

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_workflow_id_status", "workflow_id", "status"),
        Index("uq_approvals_one_pending_per_task", "task_id", unique=True,
              postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    # pending | approved | rejected | expired | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    requested_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column()
    decided_by: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    request_payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    input_overrides: Mapped[dict[str, Any] | None] = mapped_column()
    on_reject: Mapped[str | None] = mapped_column(String(32))  # fail_workflow | skip_task | inform_agent | fail_task
    # Part 9: "task" (a requires_approval task run) | "tool_call" (one proposed tool call inside a task).
    kind: Mapped[str] = mapped_column(String(16), default="task", server_default="task")
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tool_calls.id", ondelete="SET NULL"))

    workflow: Mapped[Workflow] = relationship(back_populates="approvals")


class Publication(Base):
    """One row per published artifact. idempotency_key is UNIQUE: a retry can never publish twice."""

    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    task_key: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    location: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
