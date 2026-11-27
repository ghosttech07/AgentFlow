"""Reproducibility + privacy storage (Part 12): prompt registry, per-workflow data keys, encrypted content, the PII
token vault, retention settings, erasure requests."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class WorkflowKey(Base):
    """The per-workflow data key, wrapped (AES-GCM) by the master key. Deleting it = crypto-shredding."""

    __tablename__ = "workflow_keys"

    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class StoredContent(Base):
    """An encrypted prompt / response (AUDIT_STORE_CONTENT=redacted|full). Unreadable once the key is shredded."""

    __tablename__ = "stored_contents"
    __table_args__ = (Index("ix_stored_contents_workflow", "workflow_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    llm_call_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # prompt | response
    mode: Mapped[str] = mapped_column(String(16))  # redacted | full
    sha256: Mapped[str] = mapped_column(String(64))  # of the plaintext (matches the manifest hash)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class PiiToken(Base):
    """Tokenization vault: <EMAIL_1> -> the real value (encrypted with the workflow key). value_hmac = HMAC of the
    normalized value with the master key, so an erasure can FIND a subject without decrypting anything."""

    __tablename__ = "pii_tokens"
    __table_args__ = (UniqueConstraint("workflow_id", "token", name="uq_pii_tokens_workflow_token"),
                      Index("ix_pii_tokens_value_hmac", "value_hmac"))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    token: Mapped[str] = mapped_column(String(32))
    pii_type: Mapped[str] = mapped_column(String(16))
    value_hmac: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class RetentionSetting(Base):
    """Days to keep each category for a team (None team = the default for everyone)."""

    __tablename__ = "retention_settings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), unique=True)
    days: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64))


class ErasureRequest(Base):
    """Right to erasure: two distinct approvers (Part 11 vote rules), then execution, with a report."""

    __tablename__ = "erasure_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    subject_hmacs: Mapped[list[str]] = mapped_column(JSONB)  # never the identifiers themselves
    subject_types: Mapped[list[str]] = mapped_column(JSONB)
    # the identifiers, encrypted with the master key, needed to scan stored content; wiped when completed
    subjects_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    subjects_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    reason: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | completed | rejected
    required_approvals: Mapped[int] = mapped_column(default=2)
    votes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column()
