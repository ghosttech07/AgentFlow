"""reliability: task_attempts, chaos_rules, tasks.next_attempt_at

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tstz = sa.DateTime(timezone=True)
    op.add_column("tasks", sa.Column("next_attempt_at", tstz, nullable=True))

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("started_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", tstz, nullable=True),
        sa.Column("outcome", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_attempts")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"],
                                name=op.f("fk_task_attempts_task_id_tasks"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"],
                                name=op.f("fk_task_attempts_workflow_id_workflows"), ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "attempt_number",
                            name=op.f("uq_task_attempts_task_id_attempt_number")),
    )
    op.create_index(op.f("ix_task_attempts_workflow_id"), "task_attempts", ["workflow_id"])

    op.create_table(
        "chaos_rules",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("remaining", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chaos_rules")),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"],
                                name=op.f("fk_chaos_rules_workflow_id_workflows"), ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_chaos_rules_workflow_id_task_key"), "chaos_rules", ["workflow_id", "task_key"])


def downgrade() -> None:
    op.drop_table("chaos_rules")
    op.drop_table("task_attempts")
    op.drop_column("tasks", "next_attempt_at")
