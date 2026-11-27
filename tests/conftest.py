"""Test fixtures. Tests run against the real Postgres TEST database (agentflow_test).

* Once per session: downgrade to base, then upgrade to head, so the schema comes from the
  real migrations (and the downgrade path is exercised too).
* Per test: a fresh engine (NullPool, bound to the test's event loop) and TRUNCATE of all
  tables, so tests are isolated.
* `client`: httpx AsyncClient against the app with `get_session` overridden.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

# Force test mode before the app/settings are imported anywhere.
os.environ["APP_ENV"] = "test"
os.environ["ENGINE_ENABLED"] = "false"  # tests drive the engine with run_until_idle()
os.environ["PUBLISH_DIR"] = tempfile.mkdtemp(prefix="agentflow-test-published-")  # never the repo
os.environ["ADMIN_API_KEY"] = ""  # empty = no auth (overrides .env); test_auth sets a key
os.environ["APPROVAL_WEBHOOK_URL"] = ""
os.environ["WEB_SEARCH_PROVIDER"] = "none"  # tests never touch the internet
os.environ["GEMINI_WORKER_MODEL"] = ""
# Tests inject fake clients themselves; a shell or CI LLM_PROVIDER=fake must not change what they test.
os.environ["LLM_PROVIDER"] = "gemini"

import pytest
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.config import PROJECT_ROOT, get_settings
from app.db.session import get_session
from app.engine import Engine
from app.llm import get_llm_client
from app.llm.fake_client import FakeLLMClient
from app.main import create_app

TEST_DATABASE_URL = get_settings().test_database_url
TABLES = ("workflow_events", "task_dependencies", "tasks", "workflows", "provider_usage", "asks", "memories", "users", "teams", "login_attempts")  # others cascade


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["skip_logging_config"] = True
    return cfg


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Every run leaves a JUnit XML report in reports/ (git-ignored), so a failure is always identifiable later."""
    if not config.option.xmlpath:
        from datetime import UTC, datetime

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        config.option.xmlpath = str(PROJECT_ROOT / "reports" / f"junit-{stamp}.xml")


@pytest.fixture(scope="session", autouse=True)
def sample_data_source() -> None:
    """Part 9: the read-only sample database used by sql_query (idempotent; same code as the CLI script)."""
    import asyncio

    from scripts.setup_sample_data import ensure_sample_data

    s = get_settings()
    asyncio.run(ensure_sample_data(s.database_url, s.sample_db_reader_password.get_secret_value()))


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db() -> Iterator[None]:
    """Reset the test schema via Alembic. Sync fixture: env.py runs its own event loop."""
    db_name = make_url(TEST_DATABASE_URL).database or ""
    if "test" not in db_name:  # safety net: never wipe a non-test database
        raise RuntimeError(f"Refusing to run tests against non-test database {db_name!r}")
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], fake_llm: FakeLLMClient) -> FastAPI:
    """App wired to the test DB and a FakeLLMClient, so no test can reach real Gemini."""
    application = create_app()

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = _override_get_session
    application.dependency_overrides[get_llm_client] = lambda: fake_llm
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def make_engine(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[..., Engine]:
    """Build an Engine on the test DB with the given LLM client and setting overrides.

    The engine is also attached to the app (wake-ups, /engine/status). Drive it with
    `await engine.run_until_idle()`; the background loop is never started in tests.
    """

    def _make(llm: Any, **overrides: Any) -> Engine:
        defaults = {"engine_worker_id": "test-worker", "retry_base_delay_seconds": 0.01,
                    "retry_max_delay_seconds": 0.05, "chaos_crash_delay_seconds": 0.0}
        settings = get_settings().model_copy(update={**defaults, **overrides})
        engine = Engine(session_factory, llm, settings)
        app.state.engine = engine
        return engine

    return _make
