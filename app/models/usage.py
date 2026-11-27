"""LLM usage (Part 8): one row per real model call, and per-provider quota counters.

`llm_calls` is the single source of truth for workflow totals (cost, tokens, calls), budgets,
/metrics/summary and cost-by-provider: planner calls, repairs and failed calls included.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_workflow_id", "workflow_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    role: Mapped[str | None] = mapped_column(String(32))  # planner | researcher | ... | ask
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16))  # succeeded | failed
    error_type: Mapped[str | None] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6, asdecimal=False))  # None = price unknown
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    fallback_from: Mapped[str | None] = mapped_column(String(160))
    route_position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Part 12 reproducibility: prompt template/rendered/response hashes, parameters, PII actions
    manifest: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class ProviderUsage(Base):
    """Request counter per provider:model and quota window; incremented atomically before each call."""

    __tablename__ = "provider_usage"
    __table_args__ = (UniqueConstraint("provider", "model", "window_kind", "window_start"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    window_kind: Mapped[str] = mapped_column(String(8))  # day | minute
    window_start: Mapped[datetime] = mapped_column()
    request_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())
