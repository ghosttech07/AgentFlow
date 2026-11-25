"""Best-effort approval webhook. Never blocks or breaks the engine: fire-and-forget, 5 s timeout."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Strong refs so fire-and-forget tasks are not garbage-collected mid-flight (tests await them).
pending: set[asyncio.Task[None]] = set()


async def _post(url: str, payload: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def _send(url: str, payload: dict[str, Any]) -> None:
    try:
        await _post(url, payload)
        logger.info("approval.webhook_sent approval=%s", payload.get("approval_id"))
    except Exception as exc:  # best effort: log only (the URL may contain a token, so don't log it)
        logger.warning("approval.webhook_failed approval=%s error=%s", payload.get("approval_id"),
                       type(exc).__name__)


def notify_approval(url: str | None, payload: dict[str, Any]) -> None:
    if not url:
        return
    task = asyncio.create_task(_send(url, payload))
    pending.add(task)
    task.add_done_callback(pending.discard)
