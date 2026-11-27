"""adaptive workflows: loop/branch/replan columns on tasks, task-less approvals, plan_versions, memories,
statuses REPLANNING (workflow) and SUPERSEDED (task)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WF_OLD = ("PENDING", "PLANNING", "RUNNING", "PAUSED", "AWAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED")
WF_NEW = (*WF_OLD, "REPLANNING")
TASK_OLD = ("PENDING", "READY", "RUNNING", "SUCCEEDED", "FAILED", "RETRYING", "AWAITING_APPROVAL", "CANCELLED", "SKIPPED")
TASK_NEW = (*TASK_OLD, "SUPERSEDED")


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    tstz = sa.DateTime(timezone=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.drop_constraint("workflow_status", "workflows", type_="check")
    op.create_check_constraint("workflow_status", "workflows", _in("status", WF_NEW))
    op.drop_constraint("task_status", "tasks", type_="check")
    op.create_check_constraint("task_status", "tasks", _in("status", TASK_NEW))

    op.add_column("tasks", sa.Column("loop_name", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("iteration", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.add_column("tasks", sa.Column("base_key", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("run_if", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("join", sa.String(length=16), server_default="all_succeeded", nullable=False))
    op.add_column("tasks", sa.Column("replan_if", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("skip_reason", sa.String(length=64), nullable=True))

    op.alter_column("approvals", "task_id", existing_type=sa.Uuid(), nullable=True)

    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="applied", nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("patch", jsonb, nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", tstz, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_versions")),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], name=op.f("fk_plan_versions_workflow_id_workflows"),
                                ondelete="CASCADE"),
        sa.UniqueConstraint("workflow_id", "version", name=op.f("uq_plan_versions_workflow_id_version")),
    )

    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("namespace", sa.String(length=64), server_default="default", nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("data", jsonb, nullable=True),
        sa.Column("sources", jsonb, nullable=True),
        sa.Column("source_workflow_id", sa.Uuid(), nullable=True),
        sa.Column("source_task_key", sa.String(length=64), nullable=True),
        sa.Column("tainted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("private", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", tstz, nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", tstz, nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(),
                  sa.Computed("to_tsvector('english', content)", persisted=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
        sa.ForeignKeyConstraint(["source_workflow_id"], ["workflows.id"],
                                name=op.f("fk_memories_source_workflow_id_workflows"), ondelete="SET NULL"),
    )
    op.create_index("ix_memories_search_vector", "memories", ["search_vector"], postgresql_using="gin")
    op.create_index("ix_memories_namespace_kind", "memories", ["namespace", "kind"])


def downgrade() -> None:
    op.drop_index("ix_memories_namespace_kind", table_name="memories")
    op.drop_index("ix_memories_search_vector", table_name="memories")
    op.drop_table("memories")
    op.drop_table("plan_versions")
    op.execute("DELETE FROM approvals WHERE task_id IS NULL")
    op.alter_column("approvals", "task_id", existing_type=sa.Uuid(), nullable=False)
    for col in ("skip_reason", "replan_if", "join", "run_if", "base_key", "iteration", "loop_name"):
        op.drop_column("tasks", col)
    op.execute("UPDATE tasks SET status = 'FAILED' WHERE status = 'SUPERSEDED'")
    op.execute("UPDATE workflows SET status = 'RUNNING' WHERE status = 'REPLANNING'")
    op.drop_constraint("task_status", "tasks", type_="check")
    op.create_check_constraint("task_status", "tasks", _in("status", TASK_OLD))
    op.drop_constraint("workflow_status", "workflows", type_="check")
    op.create_check_constraint("workflow_status", "workflows", _in("status", WF_OLD))
