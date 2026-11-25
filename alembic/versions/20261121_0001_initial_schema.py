"""initial schema: workflows, tasks, task_dependencies, workflow_events

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of the status values at the time of this migration. If statuses change
# later, write a new migration that drops/recreates the CHECK constraint.
WORKFLOW_STATUSES = (
    "PENDING", "PLANNING", "RUNNING", "PAUSED",
    "AWAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED",
)
TASK_STATUSES = (
    "PENDING", "READY", "RUNNING", "SUCCEEDED", "FAILED",
    "RETRYING", "AWAITING_APPROVAL", "CANCELLED", "SKIPPED",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    tstz = sa.DateTime(timezone=True)
    empty_json = sa.text("'{}'::jsonb")

    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("context", postgresql.JSONB(), server_default=empty_json, nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", tstz, nullable=True),
        sa.Column("completed_at", tstz, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
        sa.CheckConstraint(
            _in_list("status", WORKFLOW_STATUSES), name=op.f("ck_workflows_workflow_status")
        ),
    )
    op.create_index(op.f("ix_workflows_status"), "workflows", ["status"])
    op.create_index(op.f("ix_workflows_created_at"), "workflows", ["created_at"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("input", postgresql.JSONB(), server_default=empty_json, nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", tstz, nullable=True),
        sa.Column("last_heartbeat_at", tstz, nullable=True),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", tstz, nullable=True),
        sa.Column("completed_at", tstz, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"],
            name=op.f("fk_tasks_workflow_id_workflows"), ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workflow_id", "key", name=op.f("uq_tasks_workflow_id_key")),
        sa.CheckConstraint(_in_list("status", TASK_STATUSES), name=op.f("ck_tasks_task_status")),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_tasks_attempt_count_non_negative")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_tasks_max_attempts_positive")),
    )
    op.create_index(op.f("ix_tasks_workflow_id_status"), "tasks", ["workflow_id", "status"])

    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_task_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id", name=op.f("pk_task_dependencies")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_task_dependencies_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id"], ["tasks.id"],
            name=op.f("fk_task_dependencies_depends_on_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id", name=op.f("ck_task_dependencies_no_self_dependency")
        ),
    )
    op.create_index(
        op.f("ix_task_dependencies_depends_on_task_id"), "task_dependencies", ["depends_on_task_id"]
    )

    op.create_table(
        "workflow_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=empty_json, nullable=False),
        sa.Column("created_at", tstz, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_events")),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"],
            name=op.f("fk_workflow_events_workflow_id_workflows"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_workflow_events_task_id_tasks"), ondelete="CASCADE",
        ),
    )
    op.create_index(op.f("ix_workflow_events_task_id"), "workflow_events", ["task_id"])
    op.create_index(
        op.f("ix_workflow_events_workflow_id_created_at"),
        "workflow_events",
        ["workflow_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("workflow_events")
    op.drop_table("task_dependencies")
    op.drop_table("tasks")
    op.drop_table("workflows")
