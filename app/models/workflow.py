"""Workflow ORM model: one user goal and its overall execution state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import WorkflowStatus
from app.db.base import Base, status_column_type, utcnow

if TYPE_CHECKING:
    from app.models.approval import Approval
    from app.models.event import WorkflowEvent
    from app.models.task import Task


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[WorkflowStatus] = mapped_column(
        status_column_type(WorkflowStatus, "workflow_status"),
        default=WorkflowStatus.PENDING,
        server_default=WorkflowStatus.PENDING.value,
        index=True,
    )
    # Shared data passed between tasks of this workflow.
    context: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    result: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    # Set by POST /workflows run=true; the engine plans (if needed) and starts it, then clears it.
    run_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Part 8: {max_cost_usd?, max_tokens?, max_llm_calls?, max_duration_seconds?}; None = config defaults.
    budget: Mapped[dict[str, Any] | None] = mapped_column()
    # Part 11: owning team (None = global / created before teams existed) and the user who created it.
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        default=utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()

    tasks: Mapped[list[Task]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Task.created_at",
    )
    events: Mapped[list[WorkflowEvent]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowEvent.id",
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Approval.requested_at",
    )

    def __repr__(self) -> str:
        return f"<Workflow id={self.id} status={self.status}>"
