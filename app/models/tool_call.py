"""ToolCall: the durable tool journal (Part 9). One row per proposed tool step.

A row is written BEFORE a tool executes (status running) and updated after, each in its own short
transaction. idempotency_key = sha256(workflow_id|task_key|tool|args_hash) is stable across attempts and
crashes, so a replay can find the earlier outcome and never repeat an external write.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

TOOL_CALL_STATUSES = (
    "proposed", "rejected_invalid", "awaiting_approval", "approved", "denied",
    "running", "succeeded", "failed", "unknown_outcome", "superseded",
)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_task_attempt_step", "task_id", "attempt_number", "step_index"),
        Index("ix_tool_calls_idempotency_key", "idempotency_key"),
        Index("ix_tool_calls_workflow_id", "workflow_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    step_index: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any] | None] = mapped_column()  # secrets redacted
    proposed_arguments: Mapped[dict[str, Any] | None] = mapped_column()  # set when an approver changed them
    args_hash: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    side_effect: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), default="proposed", server_default="proposed")
    result: Mapped[dict[str, Any] | None] = mapped_column()  # truncated
    error: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)  # the agent's stated reason
    tainted_context: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    injection_warnings: Mapped[list[str] | None] = mapped_column(JSONB)
    reused_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tool_calls.id", ondelete="SET NULL"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column()  # approvals.id (no FK: approvals points here too)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
