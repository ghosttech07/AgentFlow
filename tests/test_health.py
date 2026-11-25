from __future__ import annotations

from collections.abc import AsyncIterator

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import get_session
from app.main import create_app


async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


async def test_health_returns_503_when_database_is_down() -> None:
    # Point the session at a port where nothing listens.
    dead = create_async_engine(
        "postgresql+asyncpg://agentflow:agentflow@127.0.0.1:1/agentflow",
        poolclass=NullPool,
        connect_args={"timeout": 2},
    )
    factory = async_sessionmaker(dead, expire_on_commit=False)
    app = create_app()

    async def _broken_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _broken_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/health")
    await dead.dispose()

    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "database": "unavailable"}
