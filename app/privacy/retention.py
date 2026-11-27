"""Retention (Part 12): per-team days per category, applied by a daily job; each deletion audited (counts only).

Categories: stored_content (encrypted prompts/responses), outputs (task outputs), memories, tool_results,
files (workspace + outbox files). A category without a value is kept forever.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import chain as audit
from app.config import Settings, get_settings
from app.db.base import utcnow
from app.models.adaptive import Memory
from app.models.privacy import RetentionSetting, StoredContent
from app.models.task import Task
from app.models.tool_call import ToolCall
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)
CATEGORIES = ("stored_content", "outputs", "memories", "tool_results", "files")


def team_namespace_filter(team_id: uuid.UUID | None) -> list[Any]:
    from app.governance.access import team_prefix

    return [Memory.namespace.startswith(team_prefix(team_id), autoescape=True)] if team_id else []


async def apply_team(session: AsyncSession, setting: RetentionSetting, settings: Settings) -> dict[str, int]:
    now = utcnow()
    team = setting.team_id
    wf_ids = select(Workflow.id).where(Workflow.team_id == team if team else Workflow.team_id.is_(None))
    counts: dict[str, int] = {}
    days = {k: int(v) for k, v in (setting.days or {}).items() if k in CATEGORIES and v is not None}
    if "stored_content" in days:
        r = await session.execute(delete(StoredContent).where(StoredContent.workflow_id.in_(wf_ids),
                                  StoredContent.created_at < now - timedelta(days=days["stored_content"])))
        counts["stored_content"] = r.rowcount or 0
    if "outputs" in days:
        r = await session.execute(update(Task).where(Task.workflow_id.in_(wf_ids), Task.output.is_not(None),
                                  Task.updated_at < now - timedelta(days=days["outputs"]))
                                  .values(output={"_erased": "retention"}))
        counts["outputs"] = r.rowcount or 0
    if "memories" in days:
        r = await session.execute(delete(Memory).where(*team_namespace_filter(team),
                                  Memory.created_at < now - timedelta(days=days["memories"])))
        counts["memories"] = r.rowcount or 0
    if "tool_results" in days:
        r = await session.execute(update(ToolCall).where(ToolCall.workflow_id.in_(wf_ids),
                                  ToolCall.created_at < now - timedelta(days=days["tool_results"]))
                                  .values(result=None))
        counts["tool_results"] = r.rowcount or 0
    if "files" in days:
        cutoff = time.time() - days["files"] * 86400
        ids = {str(w) for w in await session.scalars(wf_ids)}
        n = 0
        for base in (settings.workspaces_dir, settings.outbox_dir):
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if f.is_file() and f.stat().st_mtime < cutoff and any(i in str(f) for i in ids):
                    f.unlink(missing_ok=True)
                    n += 1
        counts["files"] = n
    if any(counts.values()):
        audit.enqueue(session, action="retention.applied", team_id=team, object_type="retention",
                      payload={"deleted": counts, "days": days})
    await session.commit()
    return counts


async def apply_all(session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None,
                    ) -> dict[str, dict[str, int]]:
    settings = settings or get_settings()
    out = {}
    async with session_factory() as s:
        rows = list(await s.scalars(select(RetentionSetting)))
    for row in rows:
        async with session_factory() as s:
            fresh = await s.get(RetentionSetting, row.id)
            out[str(row.team_id or "global")] = await apply_team(s, fresh, settings)
    return out
