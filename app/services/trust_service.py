"""Part 12 read models: the workflow manifest and the PII summary (shared by the API and the evidence export)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.canonical import digest
from app.core.errors import ForbiddenError
from app.models.attempt import TaskAttempt
from app.models.audit import AuditLog
from app.models.event import WorkflowEvent
from app.models.privacy import StoredContent, WorkflowKey
from app.models.task import Task
from app.models.usage import LLMCall
from app.services.workflow_service import get_workflow


async def manifest(session: AsyncSession, workflow_id: uuid.UUID, *, include_content: bool = False) -> dict[str, Any]:
    from app.audit import manifest as mf
    from app.governance.principal import current
    from app.privacy.crypto import ShreddedError

    wf = await get_workflow(session, workflow_id)
    tasks = list(await session.scalars(select(Task).where(Task.workflow_id == workflow_id).order_by(Task.created_at)))
    attempts = list(await session.scalars(select(TaskAttempt).where(TaskAttempt.task_id.in_([t.id for t in tasks]))
                                          .order_by(TaskAttempt.started_at)))
    calls = list(await session.scalars(select(LLMCall).where(LLMCall.workflow_id == workflow_id).order_by(LLMCall.id)))
    recorded = {r.object_id: r for r in await session.scalars(select(AuditLog).where(
        AuditLog.action == "task.manifest_recorded", AuditLog.object_id.in_([str(t.id) for t in tasks])))}
    by_task: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for a in attempts:
        by_task.setdefault(a.task_id, []).append({"attempt": a.attempt_number, "outcome": a.outcome,
                                                  "manifest": a.manifest,
                                                  "manifest_digest": digest(a.manifest) if a.manifest else None})
    stored = await session.scalar(select(StoredContent.id).where(StoredContent.workflow_id == workflow_id).limit(1))
    key = await session.get(WorkflowKey, workflow_id)
    out: dict[str, Any] = {
        "workflow_id": str(wf.id), "repro": wf.repro, "status": str(wf.status),
        "content": {"stored": stored is not None, "shredded": stored is not None and key is None},
        "tasks": [{"key": t.key, "agent_type": t.agent_type, "status": str(t.status), "attempts": by_task.get(t.id, []),
                   "audit": ({"chain_id": recorded[str(t.id)].chain_id, "seq": recorded[str(t.id)].seq,
                              "manifest_digest": recorded[str(t.id)].payload.get("manifest_digest")}
                             if str(t.id) in recorded else None)} for t in tasks],
        "llm_calls": [{"id": c.id, "role": c.role, "task_id": str(c.task_id) if c.task_id else None,
                       "provider": c.provider, "model": c.model, "outcome": c.outcome, "manifest": c.manifest}
                      for c in calls],
    }
    if include_content:
        p = current()
        if not p.at_least("admin"):
            raise ForbiddenError("Viewing stored content needs the audit.content.view permission")
        try:
            out["stored_content"] = await mf.read_content(session, workflow_id)
        except ShreddedError:
            out["stored_content"] = "shredded: the workflow's data key was deleted; the content can't be read"
        from app.audit import chain as audit

        await audit.append_now(session, action="audit.content_viewed", workflow_id=wf.id, object_type="workflow",
                               object_id=wf.id)
        await session.commit()
    return out


async def pii_summary(session: AsyncSession, workflow_id: uuid.UUID) -> dict[str, Any]:
    """Per task: what was protected and how (never the values)."""
    rows = await session.scalars(select(WorkflowEvent).where(
        WorkflowEvent.workflow_id == workflow_id,
        WorkflowEvent.event_type.in_(["privacy.protected", "privacy.routed_away", "privacy.blocked",
                                      "privacy.secret_blocked"])).order_by(WorkflowEvent.id))
    keys = {t.id: t.key for t in await session.scalars(select(Task).where(Task.workflow_id == workflow_id))}
    out: dict[str, list[dict[str, Any]]] = {}
    for e in rows:
        out.setdefault(keys.get(e.task_id, "-"), []).append({"event": e.event_type, "entry": e.payload.get("entry"),
                                                            "summary": e.payload.get("summary"),
                                                            "types": e.payload.get("types"),
                                                            "actions": e.payload.get("actions"),
                                                            "counts": e.payload.get("counts")})
    return out
