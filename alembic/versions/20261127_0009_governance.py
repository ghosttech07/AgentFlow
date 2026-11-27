"""governance: teams, users, workflow ownership, multi-approver approvals

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("name", name=op.f("uq_teams_name")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], name=op.f("fk_users_team_id_teams"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("api_key_hash", name=op.f("uq_users_api_key_hash")),
        sa.UniqueConstraint("name", name=op.f("uq_users_name")),
    )
    op.add_column("workflows", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.add_column("workflows", sa.Column("created_by", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_workflows_team_id"), "workflows", ["team_id"])
    op.create_foreign_key(op.f("fk_workflows_team_id_teams"), "workflows", "teams", ["team_id"], ["id"],
                          ondelete="SET NULL")
    op.add_column("approvals", sa.Column("required_approvals", sa.Integer(), server_default=sa.text("1"),
                                         nullable=False))
    op.add_column("approvals", sa.Column("approver_role", sa.String(length=16), nullable=True))
    op.add_column("approvals", sa.Column("votes", postgresql.JSONB(astext_type=sa.Text()),
                                         server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("approvals", sa.Column("policy_rules", postgresql.JSONB(astext_type=sa.Text()),
                                         server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    for col in ("policy_rules", "votes", "approver_role", "required_approvals"):
        op.drop_column("approvals", col)
    op.drop_constraint(op.f("fk_workflows_team_id_teams"), "workflows", type_="foreignkey")
    op.drop_index(op.f("ix_workflows_team_id"), table_name="workflows")
    op.drop_column("workflows", "created_by")
    op.drop_column("workflows", "team_id")
    op.drop_table("users")
    op.drop_table("teams")
