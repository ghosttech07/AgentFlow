"""The durable tool journal (table tool_calls).

Every row is written in its own short transaction; no transaction is ever open while a tool runs. Secrets
are redacted on the way in (arguments, results, errors). The idempotency key is stable across attempts and
crashes, so a replay can find what already happened and never repeat a write.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.models.tool_call import ToolCall
from app.tools.base import WRITE_EFFECTS
from app.tools.redact import redact

Decision = Literal["execute", "reuse", "unknown_reexecute", "unknown_needs_approval"]
_PREVIOUS_STATUSES = ("succeeded", "running", "unknown_outcome")


def args_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotency_key(workflow_id: uuid.UUID, task_key: str, tool: str, h: str) -> str:
    return hashlib.sha256(f"{workflow_id}|{task_key}|{tool}|{h}".encode()).hexdigest()


async def find_previous(session: AsyncSession, key: str) -> ToolCall | None:
    """Newest executed-or-executing row with this key (any attempt of the task)."""
    return await session.scalar(
        select(ToolCall).where(ToolCall.idempotency_key == key, ToolCall.status.in_(_PREVIOUS_STATUSES),
                               ToolCall.reused_from_id.is_(None))
        .order_by(ToolCall.created_at.desc()).limit(1))


def reuse_decision(prev: ToolCall | None, side_effect: str, replay_safe: bool, now: datetime,
                   reuse_seconds: int) -> Decision:
    if prev is None or prev.status not in _PREVIOUS_STATUSES:
        return "execute"
    is_write = side_effect in WRITE_EFFECTS
    if prev.status == "succeeded":
        if is_write:
            return "reuse"  # a write that happened is never repeated, however old
        fresh = reuse_seconds > 0 and prev.finished_at is not None and \
            prev.finished_at >= now - timedelta(seconds=reuse_seconds)
        return "reuse" if fresh else "execute"
    # running (its attempt died mid-call) or unknown_outcome: we don't know if it happened
    if not is_write:
        return "execute"
    return "unknown_reexecute" if replay_safe else "unknown_needs_approval"


def _clean(fields: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    for name in ("arguments", "proposed_arguments", "result", "error"):
        if fields.get(name) is not None:
            fields[name] = redact(fields[name], secrets)
    return fields


async def record_step(sf: async_sessionmaker[AsyncSession], *, secrets: list[str] | None = None,
                      **fields: Any) -> uuid.UUID:
    """A step that does not execute now (rejected_invalid, reused, awaiting_approval, denied, ...)."""
    fields = _clean(fields, secrets or [])
    now = utcnow()
    fields.setdefault("finished_at", now if fields.get("status") in ("succeeded", "rejected_invalid", "denied",
                                                                       "failed") else None)
    async with sf() as s:
        row = ToolCall(**fields)
        s.add(row)
        await s.commit()
        return row.id


async def record_start(sf: async_sessionmaker[AsyncSession], *, secrets: list[str] | None = None,
                       **fields: Any) -> uuid.UUID:
    """Journal a call BEFORE it executes (status running)."""
    fields = _clean(fields, secrets or [])
    async with sf() as s:
        row = ToolCall(status="running", started_at=utcnow(), **fields)
        s.add(row)
        await s.commit()
        return row.id


async def record_finish(sf: async_sessionmaker[AsyncSession], call_id: uuid.UUID, *, status: str,
                        result: dict[str, Any] | None = None, error: str | None = None,
                        duration_ms: int | None = None, secrets: list[str] | None = None,
                        **extra: Any) -> None:
    values = _clean({"result": result, "error": error}, secrets or [])
    async with sf() as s:
        await s.execute(update(ToolCall).where(ToolCall.id == call_id).values(
            status=status, result=values["result"], error=values["error"], duration_ms=duration_ms,
            finished_at=utcnow(), **extra))
        await s.commit()


async def set_status(sf: async_sessionmaker[AsyncSession], call_id: uuid.UUID, status: str, **extra: Any) -> None:
    async with sf() as s:
        await s.execute(update(ToolCall).where(ToolCall.id == call_id).values(status=status, **extra))
        await s.commit()


async def mark_unknown(sf: async_sessionmaker[AsyncSession], call_id: uuid.UUID) -> None:
    await set_status(sf, call_id, "unknown_outcome")


async def transcript(sf: async_sessionmaker[AsyncSession], task_id: uuid.UUID,
                     attempt_numbers: list[int]) -> list[ToolCall]:
    async with sf() as s:
        rows = await s.scalars(select(ToolCall).where(ToolCall.task_id == task_id,
                                                      ToolCall.attempt_number.in_(attempt_numbers))
                               .order_by(ToolCall.attempt_number, ToolCall.step_index, ToolCall.created_at))
        return list(rows)


async def executed_tool_calls(session: AsyncSession, workflow_id: uuid.UUID) -> int:
    """Tool executions that count toward max_tool_calls: started rows, not reuses."""
    return int(await session.scalar(select(func.count()).select_from(ToolCall).where(
        ToolCall.workflow_id == workflow_id, ToolCall.started_at.is_not(None),
        ToolCall.reused_from_id.is_(None))) or 0)
