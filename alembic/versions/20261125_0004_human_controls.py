"""human controls: approvals, publications

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tstz = sa.DateTime(timezone=True)
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("requested_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", tstz, nullable=True),
        sa.Column("decided_at", tstz, nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("input_overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("on_reject", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"],
                                name=op.f("fk_approvals_workflow_id_workflows"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                name=op.f("fk_approvals_task_id_tasks"), ondelete="CASCADE"),
    )
    op.create_index("ix_approvals_workflow_id_status", "approvals", ["workflow_id", "status"])
    op.create_index("uq_approvals_one_pending_per_task", "approvals", ["task_id"], unique=True,
                    postgresql_where=sa.text("status = 'pending'"))

    op.create_table(
        "publications",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("published_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"],
                                name=op.f("fk_publications_workflow_id_workflows"), ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_publications_idempotency_key")),
    )


def downgrade() -> None:
    op.drop_table("publications")
    op.drop_index("uq_approvals_one_pending_per_task", table_name="approvals")
    op.drop_index("ix_approvals_workflow_id_status", table_name="approvals")
    op.drop_table("approvals")
