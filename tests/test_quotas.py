"""Persisted provider quotas (Part 8): atomic counters, timezone windows, exhausted windows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.llm import quota
from app.llm.routes import QuotaSpec
from tests.conftest import TEST_DATABASE_URL

ENTRY = "gemini:gemini-3.5-flash"


def test_day_window_follows_timezone() -> None:
    # 2026-11-26 07:30 UTC is still 2026-11-25 in Los Angeles (UTC-8): the Google quota day.
    now = datetime(2026, 11, 26, 7, 30, tzinfo=UTC)
    assert quota.window_start("day", now, "UTC") == datetime(2026, 11, 26, tzinfo=UTC)
    la = quota.window_start("day", now, "America/Los_Angeles")
    assert la == datetime(2026, 11, 25, 8, 0, tzinfo=UTC)  # midnight LA = 08:00 UTC
    assert quota.window_start("minute", now.replace(second=42), "UTC") == now.replace(second=0)


async def test_counter_is_atomic_across_two_engines(session_factory: async_sessionmaker[AsyncSession]) -> None:
    other_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    other = async_sessionmaker(other_engine, expire_on_commit=False)
    spec = QuotaSpec(requests_per_day=10)
    settings = get_settings()
    try:
        results = await asyncio.gather(*(quota.try_reserve(sf, ENTRY, spec, settings)
                                         for sf in [session_factory, other] * 10))
    finally:
        await other_engine.dispose()
    assert sum(results) == 10  # exactly the limit, never more, under concurrency
    usage = await quota.usage_now(session_factory, ENTRY, spec, settings)
    assert usage["day"]["count"] == 10 and usage["day"]["limit"] == 10


async def test_window_rolls_over_at_midnight_in_its_timezone(session_factory: async_sessionmaker[AsyncSession]) -> None:
    spec = QuotaSpec(requests_per_day=1, timezone="America/Los_Angeles")
    s = get_settings()
    before = datetime(2026, 11, 26, 7, 59, tzinfo=UTC)  # 23:59 in LA
    after = datetime(2026, 11, 26, 8, 1, tzinfo=UTC)    # 00:01 in LA: a new quota day
    assert await quota.try_reserve(session_factory, ENTRY, spec, s, now=before)
    assert not await quota.try_reserve(session_factory, ENTRY, spec, s, now=before)
    assert await quota.try_reserve(session_factory, ENTRY, spec, s, now=after)


async def test_minute_limit(session_factory: async_sessionmaker[AsyncSession]) -> None:
    spec = QuotaSpec(requests_per_minute=2)
    s = get_settings()
    t = datetime(2026, 11, 26, 10, 0, 5, tzinfo=UTC)
    assert [await quota.try_reserve(session_factory, ENTRY, spec, s, now=t) for _ in range(3)] == [True, True, False]
    assert await quota.try_reserve(session_factory, ENTRY, spec, s, now=t.replace(minute=1))


async def test_mark_exhausted_blocks_even_with_room_left(session_factory: async_sessionmaker[AsyncSession],
                                                         caplog: pytest.LogCaptureFixture) -> None:
    spec = QuotaSpec(requests_per_day=20)
    s = get_settings()
    quota._skip_logged.clear()  # the once-per-window log memory is per process; other tests share it
    assert await quota.try_reserve(session_factory, ENTRY, spec, s)
    await quota.mark_exhausted(session_factory, ENTRY, spec, s)
    with caplog.at_level("WARNING"):
        assert not await quota.try_reserve(session_factory, ENTRY, spec, s)
        assert not await quota.try_reserve(session_factory, ENTRY, spec, s)
    assert sum("quota.skip" in r.getMessage() for r in caplog.records) == 1  # logged once per window
    usage = await quota.usage_now(session_factory, ENTRY, spec, s)
    assert usage["day"]["exhausted"] is True and usage["day"]["count"] == 1


async def test_unlimited_entries_are_still_counted(session_factory: async_sessionmaker[AsyncSession]) -> None:
    s = get_settings()
    for _ in range(3):
        assert await quota.try_reserve(session_factory, "ollama:qwen3:4b", None, s)
    assert (await quota.usage_now(session_factory, "ollama:qwen3:4b", None, s))["day"]["count"] == 3
