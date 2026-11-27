"""Tamper-evident audit log (Part 12): append-only records hash-chained per team, plus signed checkpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (UniqueConstraint("chain_id", "seq", name="uq_audit_log_chain_seq"),
                      Index("ix_audit_log_team_at", "team_id", "at"),
                      Index("ix_audit_log_object", "object_type", "object_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(BigInteger)
    team_id: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column()
    actor_type: Mapped[str] = mapped_column(String(16))  # user | token | system | agent
    actor_id: Mapped[str | None] = mapped_column(String(128))
    auth_method: Mapped[str | None] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str | None] = mapped_column(String(32))
    object_id: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[dict[str, Any] | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column()
    payload_digest: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditChainHead(Base):
    """One row per chain: the next append reads and advances it under pg_advisory_xact_lock (no forks)."""

    __tablename__ = "audit_chain_heads"

    chain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    head_hash: Mapped[str] = mapped_column(String(64))
    last_checkpoint_seq: Mapped[int] = mapped_column(BigInteger, default=-1, server_default="-1")
    last_checkpoint_at: Mapped[datetime | None] = mapped_column()
    verified_seq: Mapped[int] = mapped_column(BigInteger, default=-1, server_default="-1")
    verified_at: Mapped[datetime | None] = mapped_column()
    integrity_ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    integrity_error: Mapped[str | None] = mapped_column(Text)


class AuditCheckpoint(Base):
    """A signed statement "chain X had head H at seq N" (Ed25519); also exported outside the DB."""

    __tablename__ = "audit_checkpoints"
    __table_args__ = (UniqueConstraint("chain_id", "seq", name="uq_audit_checkpoints_chain_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chain_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(BigInteger)
    head_hash: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    key_id: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(Text)  # base64 Ed25519 over canonical_json({chain_id, seq, head_hash, at, key_id})
