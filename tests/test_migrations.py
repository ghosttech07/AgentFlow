"""Guards against drift between the ORM models and the Alembic migrations."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
import app.models  # noqa: F401


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    def _diff(sync_conn):  # type: ignore[no-untyped-def]
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    # alembic_version is managed by Alembic itself, not part of the models.
    diff = [d for d in diff if not (d[0] == "remove_table" and d[1].name == "alembic_version")]
    assert diff == [], f"Models and migrations differ - create a new migration: {diff}"
