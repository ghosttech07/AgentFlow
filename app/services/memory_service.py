"""Memory between workflows (Part 10). CODE decides what is stored (never the model):

- researcher SUCCEEDED -> one `fact` per key finding, only if the task has VERIFIED sources (stored with them);
- a loop ends after more than one iteration -> a `lesson` from the reviewer's recurring issues;
- a workflow COMPLETES -> an `artifact_summary`.
Memories carry the source task's taint / private flags. Reads use Postgres full-text search (ranked by relevance,
recency, confidence; pinned first); a tainted memory taints the task that reads it.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import Float, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.events import EventType, record_event
from app.db.base import utcnow
from app.models.adaptive import Memory
from app.models.task import Task
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)
WORD_RE = re.compile(r"[A-Za-z0-9]{3,}")
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.8, "low": 0.5}
MIN_RANK = 0.01


def _switch(wf: Workflow, which: str, settings: Settings) -> bool:
    if not settings.memory_enabled:
        return False
    return bool(((wf.context or {}).get("memory") or {}).get(which, True))


def _namespace(wf: Workflow) -> str:
    return ((wf.context or {}).get("memory") or {}).get("namespace", "default")


def _private(task: Task) -> bool:
    return bool(((task.metrics or {}).get("tools") or {}).get("private"))


is_private = _private


async def write_from_task(session: AsyncSession, wf: Workflow, task: Task, settings: Settings | None = None) -> int:
    """Facts from a succeeded researcher (no commit). Returns how many were stored."""
    settings = settings or get_settings()
    if task.agent_type != "researcher" or not _switch(wf, "write", settings):
        return 0
    out = task.output or {}
    sources = [s for s in out.get("sources") or [] if isinstance(s, dict) and s.get("url")]
    if not sources:  # only facts backed by verified sources are worth remembering
        return 0
    if out.get("from_memory") and not out.get("key_findings"):
        return 0
    expires = utcnow() + timedelta(days=settings.memory_fact_ttl_days)
    stored = 0
    for finding in (out.get("key_findings") or [])[:10]:
        text = str(finding).strip()
        if not text or text in (out.get("from_memory") or []):
            continue  # facts reused from memory are not stored again
        session.add(Memory(namespace=_namespace(wf), kind="fact", content=text,
                           data={"task_summary": out.get("summary"), "goal": wf.goal},
                           sources=[{"title": s.get("title"), "url": s["url"]} for s in sources],
                           source_workflow_id=wf.id, source_task_key=task.key, tainted=bool(task.tainted),
                           private=_private(task), confidence=out.get("confidence"), expires_at=expires))
        stored += 1
    return stored


async def write_lesson(session: AsyncSession, wf: Workflow, reviews: list[dict[str, Any]],
                       settings: Settings | None = None, *, tainted: bool = False, private: bool = False) -> bool:
    """A lesson inherits taint / privacy from the loop tasks it was learned from (review text can echo a page)."""
    settings = settings or get_settings()
    if len(reviews) < 2 or not _switch(wf, "write", settings):
        return False
    issues = [str(i.get("description", "")) for r in reviews[:-1] for i in (r.get("issues") or [])
              if i.get("severity") in ("high", "medium")]
    if not issues:
        return False
    session.add(Memory(namespace=_namespace(wf), kind="lesson",
                       content=f"First drafts for goals like '{wf.goal[:120]}' often missed: " + "; ".join(issues[:5]),
                       data={"iterations": len(reviews)}, source_workflow_id=wf.id, confidence="medium",
                       tainted=tainted, private=private,
                       expires_at=utcnow() + timedelta(days=settings.memory_lesson_ttl_days)))
    return True


async def write_artifact_summary(session: AsyncSession, wf: Workflow, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not _switch(wf, "write", settings) or not wf.result:
        return False
    tasks = list(await session.scalars(select(Task).where(Task.workflow_id == wf.id,
                                                          Task.status == "SUCCEEDED")))
    tainted = any(t.tainted for t in tasks)  # the summary is built from these outputs: it carries their flags
    private = any(_private(t) for t in tasks)
    parts = []
    for key, out in (wf.result.get("final_outputs") or {}).items():
        if isinstance(out, dict):
            parts.append(str(out.get("title") or out.get("summary") or key))
    session.add(Memory(namespace=_namespace(wf), kind="artifact_summary",
                       content=f"{wf.goal[:200]} -> {'; '.join(parts)[:400]}",
                       data={"summary": wf.result.get("summary")}, source_workflow_id=wf.id, confidence="medium",
                       tainted=tainted, private=private,
                       expires_at=utcnow() + timedelta(days=settings.memory_lesson_ttl_days)))
    return True


def _tsquery(text: str) -> str | None:
    words = sorted({w.lower() for w in WORD_RE.findall(text)})[:40]
    return " | ".join(words) if words else None


async def search(session: AsyncSession, query: str, *, namespace: str = "default", k: int = 5,
                 kind: str | None = None, include_expired: bool = False) -> list[Memory]:
    """Full-text search (OR of the query words), ranked by relevance x recency x confidence; pinned first."""
    tsq = _tsquery(query)
    stmt = select(Memory).where(Memory.deleted_at.is_(None), Memory.namespace == namespace)
    if kind:
        stmt = stmt.where(Memory.kind == kind)
    if not include_expired:
        stmt = stmt.where((Memory.expires_at.is_(None)) | (Memory.expires_at > utcnow()))
    if tsq is None:
        return list(await session.scalars(stmt.order_by(Memory.pinned.desc(), Memory.created_at.desc()).limit(k)))
    q = func.to_tsquery("english", tsq)
    rank = func.ts_rank(Memory.search_vector, q)
    age_days = func.extract("epoch", func.now() - Memory.created_at) / 86400.0
    recency = 1.0 / (1.0 + cast(age_days, Float) / 30.0)
    rows = (await session.execute(
        stmt.where(Memory.search_vector.op("@@")(q)).add_columns(rank * recency)
        .order_by(Memory.pinned.desc(), (rank * recency).desc()).limit(k * 3))).all()
    ranked = sorted(((m, float(score) * CONFIDENCE_WEIGHT.get(m.confidence or "", 0.7)) for m, score in rows),
                    key=lambda x: (not x[0].pinned, -x[1]))
    return [m for m, score in ranked if score >= MIN_RANK or m.pinned][:k]


async def for_task(session: AsyncSession, wf: Workflow, task: Task, settings: Settings | None = None) -> list[Memory]:
    settings = settings or get_settings()
    if not _switch(wf, "read", settings) or settings.memory_top_k == 0:
        return []
    found = await search(session, f"{wf.goal} {task.name} {task.description}", namespace=_namespace(wf),
                         k=settings.memory_top_k)
    return [m for m in found if m.source_workflow_id != wf.id]  # earlier workflows only


def as_prompt(memories: list[Memory]) -> str:
    """PRIOR KNOWLEDGE section; tainted memories are delimited as untrusted data."""
    lines = ["## PRIOR KNOWLEDGE (from earlier workflows; may be outdated; verify if important)"]
    for m in memories:
        when = m.created_at.date().isoformat() if m.created_at else "?"
        srcs = ", ".join(s.get("url", "") for s in (m.sources or [])[:3])
        body = f"- [{m.kind}, {when}, confidence {m.confidence or '?'}] {m.content}" + (f" (sources: {srcs})" if srcs else "")
        if m.tainted:
            body = f'<untrusted_tool_result source="memory {m.id}">\n{body}\n</untrusted_tool_result>'
        lines.append(body)
    return "\n".join(lines)


async def get(session: AsyncSession, memory_id: uuid.UUID) -> Memory:
    m = await session.get(Memory, memory_id)
    if m is None or m.deleted_at is not None:
        raise NotFoundError(f"Memory {memory_id} not found")
    return m


async def pin(session: AsyncSession, memory_id: uuid.UUID, pinned: bool = True) -> Memory:
    m = await get(session, memory_id)
    m.pinned = pinned
    await session.commit()
    return m


async def soft_delete(session: AsyncSession, memory_id: uuid.UUID, actor: str) -> None:
    m = await get(session, memory_id)
    m.deleted_at = utcnow()
    if m.source_workflow_id:
        record_event(session, workflow_id=m.source_workflow_id, event_type=EventType.MEMORY_DELETED,
                     payload={"memory_id": str(m.id), "actor": actor, "content": m.content[:200]})
    await session.commit()


async def purge_expired(session: AsyncSession) -> int:
    res = await session.execute(update(Memory).where(Memory.deleted_at.is_(None), Memory.expires_at.is_not(None),
                                                     Memory.expires_at <= utcnow()).values(deleted_at=utcnow()))
    await session.commit()
    return int(res.rowcount or 0)


def to_dict(m: Memory) -> dict[str, Any]:
    return {"id": str(m.id), "namespace": m.namespace, "kind": m.kind, "content": m.content, "sources": m.sources or [],
            "source_workflow_id": str(m.source_workflow_id) if m.source_workflow_id else None,
            "source_task_key": m.source_task_key, "tainted": m.tainted, "private": m.private,
            "confidence": m.confidence, "pinned": m.pinned, "created_at": m.created_at.isoformat(),
            "expires_at": m.expires_at.isoformat() if m.expires_at else None}
