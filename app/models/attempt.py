"""TaskAttempt (one row per execution attempt) and ChaosRule (dev-only fault injection)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class TaskAttempt(Base):
    """Attempt history: the crash demo's proof (attempt 1 = worker_lost, attempt 2 = succeeded).

    attempt_number keeps increasing for a task even when a manual /retry resets attempt_count.
    """

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number"),
        Index("ix_task_attempts_workflow_id", "workflow_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()
    # running | succeeded | failed | worker_lost | interrupted | cancelled | budget_paused | suspended
    outcome: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    error_type: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(32))  # Part 8
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6, asdecimal=False))  # Part 8; None = unknown
    # Part 9: tool-loop checkpoint (taint, untrusted sources, step counter, pending tool call) for a resume.
    loop_state: Mapped[dict[str, Any] | None] = mapped_column()


class ChaosRule(Base):
    """Persisted fault injection for one task (dev only). `remaining` is decremented and committed
    BEFORE the fault fires, so a crash_process rule does not fire again after the restart."""

    __tablename__ = "chaos_rules"
    __table_args__ = (Index("ix_chaos_rules_workflow_id_task_key", "workflow_id", "task_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    task_key: Mapped[str] = mapped_column(String(64))
    # crash_process | raise_transient | raise_overloaded | raise_permanent | hang
    mode: Mapped[str] = mapped_column(String(32))
    remaining: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
