"""Dev-only fault injection, driven by persisted ChaosRule rows (disabled when APP_ENV=production).

Modes: crash_process (hard os._exit after CHAOS_CRASH_DELAY_SECONDS), raise_transient,
raise_overloaded, raise_permanent, hang (longer than TASK_TIMEOUT_SECONDS).
`remaining` is decremented and COMMITTED before the fault fires, so after a crash + restart the
retried task runs normally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.llm.errors import LLMOverloadedError, LLMPermanentError, LLMTransientError
from app.models.attempt import ChaosRule

logger = logging.getLogger(__name__)

MODES = ("crash_process", "raise_transient", "raise_overloaded", "raise_permanent", "hang")
AFTER_TOOL = "crash_after_tool:"  # Part 9: crash right after a named tool call succeeded (journaled)


def _hard_exit(code: int) -> None:
    """Kill the process with no cleanup at all (like a real crash). Patched in tests."""
    logging.shutdown()
    os._exit(code)


async def inject(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_id: uuid.UUID,
    task_key: str,
    settings: Settings,
) -> None:
    """Fire the first active chaos rule for this task, if any. No-op in production."""
    if settings.app_env == "production":
        return
    async with session_factory() as session:
        rule = await session.scalar(_active_rule(workflow_id, task_key))
    if rule is None:
        return
    if rule.mode == "crash_process":
        # Give parallel siblings time to be mid-flight or finished, so the demo shows both.
        await asyncio.sleep(settings.chaos_crash_delay_seconds)

    async with session_factory() as session:  # decrement + COMMIT before the fault fires
        rule = await session.scalar(_active_rule(workflow_id, task_key).with_for_update())
        if rule is None:
            return
        rule.remaining -= 1
        mode = rule.mode
        await session.commit()

    logger.warning("chaos.fire mode=%s task_key=%s workflow=%s", mode, task_key, workflow_id)
    if mode == "crash_process":
        logger.critical("chaos.crash_process: hard-exiting the server now (exit code 1)")
        _hard_exit(1)
    elif mode == "raise_transient":
        raise LLMTransientError("Chaos: injected transient error")
    elif mode == "raise_overloaded":
        raise LLMOverloadedError("Chaos: injected overload (503 high demand)")
    elif mode == "raise_permanent":
        raise LLMPermanentError("Chaos: injected permanent error")
    elif mode == "hang":
        await asyncio.sleep(settings.task_timeout_seconds + 60)


async def has_rule(
    session: AsyncSession, workflow_id: uuid.UUID, task_key: str, settings: Settings
) -> bool:
    """Cheap pre-check in the worker's existing session (avoids a connection per task)."""
    if settings.app_env == "production":
        return False
    return await session.scalar(_active_rule(workflow_id, task_key)) is not None


def _active_rule(workflow_id: uuid.UUID, task_key: str, mode: str | None = None):  # noqa: ANN202
    stmt = (select(ChaosRule)
            .where(ChaosRule.workflow_id == workflow_id, ChaosRule.task_key == task_key, ChaosRule.remaining > 0)
            .order_by(ChaosRule.created_at).limit(1))
    if mode is not None:
        return stmt.where(ChaosRule.mode == mode)
    return stmt.where(~ChaosRule.mode.startswith(AFTER_TOOL))  # agent-start faults only


async def after_tool(session_factory: async_sessionmaker[AsyncSession], workflow_id: uuid.UUID, task_key: str,
                     tool: str, settings: Settings) -> None:
    """crash_after_tool:<tool>: hard-exit right after that call succeeded and its journal row was committed:
    the worst moment for a side effect (it happened, but the task result was never saved)."""
    if settings.app_env == "production":
        return
    mode = AFTER_TOOL + tool
    async with session_factory() as session:
        rule = await session.scalar(_active_rule(workflow_id, task_key, mode).with_for_update())
        if rule is None:
            return
        rule.remaining -= 1
        await session.commit()
    logger.critical("chaos.crash_after_tool tool=%s task_key=%s: hard-exiting the server now", tool, task_key)
    _hard_exit(1)
