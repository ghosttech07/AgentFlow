"""audit: append-only hash-chained audit_log, chain heads, signed checkpoints; backfill of existing events

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = """
CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit tables are append-only (% on %)', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chain_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("team_id", sa.String(length=64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("auth_method", sa.String(length=32), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=True),
        sa.Column("object_id", sa.String(length=64), nullable=True),
        sa.Column("decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("chain_id", "seq", name="uq_audit_log_chain_seq"),
    )
    op.create_index("ix_audit_log_team_at", "audit_log", ["team_id", "at"])
    op.create_index("ix_audit_log_object", "audit_log", ["object_type", "object_id"])
    op.create_table(
        "audit_chain_heads",
        sa.Column("chain_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("head_hash", sa.String(length=64), nullable=False),
        sa.Column("last_checkpoint_seq", sa.BigInteger(), server_default="-1", nullable=False),
        sa.Column("last_checkpoint_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_seq", sa.BigInteger(), server_default="-1", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("integrity_ok", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("integrity_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("chain_id", name=op.f("pk_audit_chain_heads")),
    )
    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chain_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("head_hash", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_checkpoints")),
        sa.UniqueConstraint("chain_id", "seq", name="uq_audit_checkpoints_chain_seq"),
    )
    op.execute(APPEND_ONLY)
    for table in ("audit_log", "audit_checkpoints"):
        op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION audit_append_only()")
    _backfill()


def _backfill() -> None:
    """Existing events become audit records (payload.backfilled = true), per chain in their original order."""
    from datetime import UTC, datetime

    from app.audit.canonical import GENESIS_PREV, digest, record_hash
    from app.privacy.redact import redact_obj

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT e.id, e.workflow_id, e.task_id, e.event_type, e.payload, e.created_at, w.team_id "
        "FROM workflow_events e JOIN workflows w ON w.id = e.workflow_id ORDER BY e.id")).fetchall()
    heads: dict[str, tuple[int, str]] = {}
    ins = sa.text("INSERT INTO audit_log (chain_id, seq, team_id, at, actor_type, actor_id, auth_method, ip, action, "
                  "object_type, object_id, decision, payload, payload_digest, prev_hash, hash) VALUES (:chain_id, "
                  ":seq, :team_id, :at, :actor_type, :actor_id, :auth_method, :ip, :action, :object_type, :object_id, "
                  "CAST(:decision AS jsonb), CAST(:payload AS jsonb), :payload_digest, :prev_hash, :hash)")
    import json

    def put(rec: dict) -> str:
        h = record_hash(rec, rec["prev_hash"])
        bind.execute(ins, {**rec, "decision": json.dumps(rec["decision"]) if rec["decision"] is not None else None,
                           "payload": json.dumps(rec["payload"], default=str), "hash": h})
        return h

    for r in rows:
        chain = str(r.team_id) if r.team_id else "system"
        if chain not in heads:
            g = {"chain_id": chain, "seq": 0, "at": datetime.now(UTC), "team_id": None if chain == "system" else chain,
                 "actor_type": "system", "actor_id": "engine", "auth_method": None, "ip": None,
                 "action": "chain.genesis", "object_type": "chain", "object_id": chain, "decision": None,
                 "payload": {"chain_id": chain}, "payload_digest": digest({"chain_id": chain}),
                 "prev_hash": GENESIS_PREV}
            heads[chain] = (0, put(g))
        seq, prev = heads[chain]
        original = {"workflow_id": str(r.workflow_id), **({"task_id": str(r.task_id)} if r.task_id else {}),
                    **(r.payload or {}), "backfilled": True}
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)
        rec = {"chain_id": chain, "seq": seq + 1, "at": created, "team_id": None if chain == "system" else chain,
               "actor_type": "system", "actor_id": "backfill", "auth_method": None, "ip": None,
               "action": r.event_type[:64], "object_type": "task" if r.task_id else "workflow",
               "object_id": str(r.task_id or r.workflow_id), "decision": None,
               "payload": redact_obj(original), "payload_digest": digest(original), "prev_hash": prev}
        heads[chain] = (seq + 1, put(rec))
    for chain, (seq, h) in heads.items():
        bind.execute(sa.text("INSERT INTO audit_chain_heads (chain_id, seq, head_hash) VALUES (:c, :s, :h)"),
                     {"c": chain, "s": seq, "h": h})


def downgrade() -> None:
    for table in ("audit_log", "audit_checkpoints"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS audit_append_only()")
    op.drop_table("audit_checkpoints")
    op.drop_table("audit_chain_heads")
    op.drop_index("ix_audit_log_object", table_name="audit_log")
    op.drop_index("ix_audit_log_team_at", table_name="audit_log")
    op.drop_table("audit_log")
