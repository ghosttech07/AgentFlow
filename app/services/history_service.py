"""History: saved Quick Ask questions + research workflows, newest first; per team since Part 11."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import AppError, NotFoundError
from app.llm.base import LLMClient
from app.models.ask import Ask
from app.models.workflow import Workflow
from app.services import ask_service


async def ask_and_save(session: AsyncSession, question: str, llm: LLMClient, settings: Settings) -> dict[str, Any]:
    """Answer the question and save it (answered or failed) so it shows up in the history."""
    start = time.monotonic()
    from app.governance.principal import current

    caller = current()
    row = Ask(id=uuid.uuid4(), question=question, status="answered", team_id=caller.team_id,
              created_by=caller.name if caller.kind == "user" else None)
    try:
        result = await ask_service.ask(question, llm, settings)
    except AppError as exc:
        row.status, row.error = "failed", exc.message
        row.duration_ms = int((time.monotonic() - start) * 1000)
        session.add(row)
        await session.commit()
        raise
    row.result = result
    row.duration_ms = int((time.monotonic() - start) * 1000)
    session.add(row)
    await session.commit()
    return {"id": str(row.id), **result}


async def get_ask(session: AsyncSession, ask_id: uuid.UUID) -> dict[str, Any]:
    from app.governance.principal import current

    row = await session.get(Ask, ask_id)
    if row is None or (not current().is_global and row.team_id != current().team_id):
        raise NotFoundError(f"Question {ask_id} not found")
    return {"id": str(row.id), "question": row.question, "status": row.status, "result": row.result,
            "error": row.error, "duration_ms": row.duration_ms, "created_at": row.created_at.isoformat()}


async def list_history(session: AsyncSession, limit: int) -> list[dict[str, Any]]:
    from app.governance.access import workflow_filters
    from app.governance.principal import current

    p = current()
    ask_filter = [] if p.is_global else [Ask.team_id == p.team_id]
    asks = (await session.scalars(select(Ask).where(*ask_filter).order_by(Ask.created_at.desc()).limit(limit))).all()
    workflows = (await session.scalars(select(Workflow).where(*workflow_filters(p))
                                       .order_by(Workflow.created_at.desc()).limit(limit))).all()
    items = [{"kind": "question", "id": str(a.id), "title": a.question, "status": a.status, "error": a.error,
              "created_at": a.created_at} for a in asks]
    items += [{"kind": "research", "id": str(w.id), "title": w.goal or w.name, "status": str(w.status),
               "error": w.error, "created_at": w.created_at} for w in workflows]
    items.sort(key=lambda i: i["created_at"], reverse=True)
    return [{**i, "created_at": i["created_at"].isoformat()} for i in items[:limit]]
