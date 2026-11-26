"""cost control: llm_calls, provider_usage, workflows.budget, task_attempts cost/provider

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tstz = sa.DateTime(timezone=True)
    op.add_column("workflows", sa.Column("budget", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("task_attempts", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column("task_attempts", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("fallback_from", sa.String(length=160), nullable=True),
        sa.Column("route_position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"],
                                name=op.f("fk_llm_calls_workflow_id_workflows"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                name=op.f("fk_llm_calls_task_id_tasks"), ondelete="CASCADE"),
    )
    op.create_index("ix_llm_calls_workflow_id", "llm_calls", ["workflow_id"])

    op.create_table(
        "provider_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("window_kind", sa.String(length=8), nullable=False),
        sa.Column("window_start", tstz, nullable=False),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exhausted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("updated_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_usage")),
        sa.UniqueConstraint("provider", "model", "window_kind", "window_start",
                            name=op.f("uq_provider_usage_provider_model_window_kind_window_start")),
    )


def downgrade() -> None:
    op.drop_table("provider_usage")
    op.drop_index("ix_llm_calls_workflow_id", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_column("task_attempts", "cost_usd")
    op.drop_column("task_attempts", "provider")
    op.drop_column("workflows", "budget")
