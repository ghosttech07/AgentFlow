"""WorkflowEvent ORM model: append-only audit log of everything that happens to a workflow."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow

if TYPE_CHECKING:
    from app.models.workflow import Workflow


class WorkflowEvent(Base):
    """Rows are only ever inserted, never updated. The bigint id gives a strict total order."""

    __tablename__ = "workflow_events"
    __table_args__ = (Index("ix_workflow_events_workflow_id_created_at", "workflow_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    workflow: Mapped[Workflow] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<WorkflowEvent id={self.id} type={self.event_type}>"
