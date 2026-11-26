"""tools: tool_calls journal, approvals.kind/tool_call_id, task_attempts.loop_state, tasks.tainted

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tstz = sa.DateTime(timezone=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("arguments", jsonb, nullable=True),
        sa.Column("proposed_arguments", jsonb, nullable=True),
        sa.Column("args_hash", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("side_effect", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="proposed", nullable=False),
        sa.Column("result", jsonb, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("tainted_context", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("injection_warnings", jsonb, nullable=True),
        sa.Column("reused_from_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", tstz, nullable=True),
        sa.Column("finished_at", tstz, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"],
                                name=op.f("fk_tool_calls_workflow_id_workflows"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name=op.f("fk_tool_calls_task_id_tasks"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reused_from_id"], ["tool_calls.id"],
                                name=op.f("fk_tool_calls_reused_from_id_tool_calls"), ondelete="SET NULL"),
    )
    op.create_index("ix_tool_calls_task_attempt_step", "tool_calls", ["task_id", "attempt_number", "step_index"])
    op.create_index("ix_tool_calls_idempotency_key", "tool_calls", ["idempotency_key"])
    op.create_index("ix_tool_calls_workflow_id", "tool_calls", ["workflow_id"])
    op.add_column("approvals", sa.Column("kind", sa.String(length=16), server_default="task", nullable=False))
    op.add_column("approvals", sa.Column("tool_call_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f("fk_approvals_tool_call_id_tool_calls"), "approvals", "tool_calls",
                          ["tool_call_id"], ["id"], ondelete="SET NULL")
    op.add_column("task_attempts", sa.Column("loop_state", jsonb, nullable=True))
    op.add_column("tasks", sa.Column("tainted", sa.Boolean(), server_default=sa.text("false"), nullable=False))


def downgrade() -> None:
    op.drop_column("tasks", "tainted")
    op.drop_column("task_attempts", "loop_state")
    op.drop_constraint(op.f("fk_approvals_tool_call_id_tool_calls"), "approvals", type_="foreignkey")
    op.drop_column("approvals", "tool_call_id")
    op.drop_column("approvals", "kind")
    op.drop_index("ix_tool_calls_workflow_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_idempotency_key", table_name="tool_calls")
    op.drop_index("ix_tool_calls_task_attempt_step", table_name="tool_calls")
    op.drop_table("tool_calls")
