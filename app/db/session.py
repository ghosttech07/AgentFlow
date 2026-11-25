"""Async engine and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def build_engine(database_url: str) -> AsyncEngine:
    """Create an async engine. `pool_pre_ping` drops dead connections after a DB restart."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        # asyncpg connect timeout (seconds) so /health fails fast when the DB is down.
        connect_args={"timeout": 5},
    )


# The engine connects lazily, so creating it at import time does not touch the database.
engine: AsyncEngine = build_engine(get_settings().database_url)

# expire_on_commit=False: objects stay usable after commit (no implicit lazy reloads,
# which are not allowed under asyncio).
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request. Services own commit/rollback."""
    async with SessionLocal() as session:
        yield session
