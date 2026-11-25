"""Persisted per-provider quotas (Part 8).

Before every real call the router reserves one request in the entry's day window (always counted,
for /providers) and, when configured, its minute window. A reservation is ONE atomic statement:

  INSERT ... ON CONFLICT (provider, model, window_kind, window_start)
  DO UPDATE SET request_count = request_count + 1
  WHERE request_count < :limit AND NOT exhausted
  RETURNING request_count

No row returned = the window is full or marked exhausted, so concurrent engines can never exceed the
limit. Failed calls stay counted (providers count them too). A provider's quota_exhausted answer
marks the day window exhausted, so the router stops trying until the window rolls over at midnight
in the entry's timezone (QuotaSpec.timezone, else QUOTA_TIMEZONE; Google resets at midnight Pacific).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.llm.routes import QuotaSpec, split_entry
from app.models.usage import ProviderUsage

logger = logging.getLogger(__name__)

_skip_logged: set[tuple[str, str, datetime]] = set()  # log a blocked window once (per process)


def window_start(kind: str, now: datetime, tz: str) -> datetime:
    """Start of the window containing `now`, in UTC. Day windows start at local midnight in `tz`."""
    if kind == "minute":
        return now.astimezone(UTC).replace(second=0, microsecond=0)
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _tz(spec: QuotaSpec | None, settings: Settings) -> str:
    return (spec.timezone if spec and spec.timezone else None) or settings.quota_timezone


def _windows(spec: QuotaSpec | None, settings: Settings, now: datetime) -> list[tuple[str, datetime, int | None]]:
    tz = _tz(spec, settings)
    windows = [("day", window_start("day", now, tz), spec.requests_per_day if spec else None)]
    if spec and spec.requests_per_minute:
        windows.append(("minute", window_start("minute", now, tz), spec.requests_per_minute))
    return windows


async def _reserve_one(session: AsyncSession, provider: str, model: str, kind: str, start: datetime,
                       limit: int | None) -> bool:
    table = ProviderUsage.__table__
    stmt = pg_insert(ProviderUsage).values(provider=provider, model=model, window_kind=kind, window_start=start,
                                           request_count=1, exhausted=False)
    condition = ~table.c.exhausted
    if limit is not None:
        condition = and_(condition, table.c.request_count < limit)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "model", "window_kind", "window_start"],
        set_={"request_count": table.c.request_count + 1, "updated_at": datetime.now(UTC)},
        where=condition,
    ).returning(table.c.request_count)
    return (await session.execute(stmt)).first() is not None


async def try_reserve(sf: async_sessionmaker[AsyncSession], entry: str, spec: QuotaSpec | None,
                      settings: Settings, now: datetime | None = None) -> bool:
    """Reserve one request for `entry` now. False = quota used up or window marked exhausted."""
    now = now or datetime.now(UTC)
    provider, model = split_entry(entry)
    async with sf() as session:
        for kind, start, limit in _windows(spec, settings, now):
            if not await _reserve_one(session, provider, model, kind, start, limit):
                await session.rollback()  # undo an earlier window's increment in this reservation
                key = (entry, kind, start)
                if key not in _skip_logged:
                    _skip_logged.add(key)
                    logger.warning("quota.skip entry=%s window=%s start=%s limit=%s (used up or exhausted)",
                                   entry, kind, start.isoformat(), limit)
                return False
        await session.commit()
    return True


async def mark_exhausted(sf: async_sessionmaker[AsyncSession], entry: str, spec: QuotaSpec | None,
                         settings: Settings, now: datetime | None = None) -> None:
    """The provider said quota_exhausted: block this entry for the rest of its day window."""
    now = now or datetime.now(UTC)
    provider, model = split_entry(entry)
    start = window_start("day", now, _tz(spec, settings))
    async with sf() as session:
        stmt = pg_insert(ProviderUsage).values(provider=provider, model=model, window_kind="day",
                                               window_start=start, request_count=0, exhausted=True)
        await session.execute(stmt.on_conflict_do_update(
            index_elements=["provider", "model", "window_kind", "window_start"],
            set_={"exhausted": True, "updated_at": datetime.now(UTC)}))
        await session.commit()
    logger.warning("quota.exhausted entry=%s until the day window ends (starts %s)", entry, start.isoformat())


async def usage_now(sf: async_sessionmaker[AsyncSession], entry: str, spec: QuotaSpec | None,
                    settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    provider, model = split_entry(entry)
    tz = _tz(spec, settings)
    out: dict[str, Any] = {}
    async with sf() as session:
        for kind, limit in (("day", spec.requests_per_day if spec else None),
                            ("minute", spec.requests_per_minute if spec else None)):
            start = window_start(kind, now, tz)
            row = await session.scalar(select(ProviderUsage).where(
                ProviderUsage.provider == provider, ProviderUsage.model == model,
                ProviderUsage.window_kind == kind, ProviderUsage.window_start == start))
            out[kind] = {"count": row.request_count if row else 0, "limit": limit,
                         "exhausted": bool(row and row.exhausted), "window_start": start.isoformat(),
                         "timezone": tz}
    return out


__all__ = ["mark_exhausted", "try_reserve", "usage_now", "window_start"]
