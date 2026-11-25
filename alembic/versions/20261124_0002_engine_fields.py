"""engine fields: tasks.metrics, tasks.error_type, workflows.run_requested

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("metrics", postgresql.JSONB(), nullable=True))
    op.add_column("tasks", sa.Column("error_type", sa.String(length=32), nullable=True))
    op.add_column(
        "workflows",
        sa.Column("run_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("workflows", "run_requested")
    op.drop_column("tasks", "error_type")
    op.drop_column("tasks", "metrics")
