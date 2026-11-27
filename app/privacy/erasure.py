"""Right to erasure (Part 12): two-person approval, then find, delete, crypto-shred, report.

Find: the tokenization vault (HMAC of the normalized identifier: no decryption) + scans of task inputs/outputs,
tool-call arguments/results, memories and the decrypted stored content of the team's workflows.
Erase: memories deleted, task outputs/inputs and tool-call data scrubbed, workspace/outbox files deleted, event
payloads scrubbed; the affected workflows' data keys are DELETED (crypto-shredding), so their encrypted prompts,
responses and vault entries can never be read again. The audit chain stays verifiable: it only holds hashes and
redacted payloads. Published files outside AgentFlow are listed for manual handling.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import chain as audit
from app.config import Settings, get_settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.db.base import utcnow
from app.models.adaptive import Memory
from app.models.event import WorkflowEvent
from app.models.privacy import ErasureRequest, PiiToken, StoredContent, WorkflowKey
from app.models.task import Task
from app.models.tool_call import ToolCall
from app.models.workflow import Workflow
from app.privacy import crypto
from app.privacy.detectors import types_in

ERASED = "[ERASED]"


def _norm(v: str) -> str:
    return re.sub(r"[\s\-]", "", v.strip().lower())


def _variants(values: list[str]) -> list[re.Pattern[str]]:
    """Match the identifier with or without spaces/dashes (phone and Aadhaar are written both ways)."""
    pats = []
    for v in values:
        core = _norm(v)
        pats.append(re.compile(r"[\s\-]?".join(re.escape(c) for c in core), re.IGNORECASE))
    return pats


def _contains(obj: Any, pats: list[re.Pattern[str]]) -> bool:
    text = json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj
    return any(p.search(text) for p in pats)


def _scrub(obj: Any, pats: list[re.Pattern[str]]) -> Any:
    if isinstance(obj, str):
        for p in pats:
            obj = p.sub(ERASED, obj)
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, pats) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, pats) for v in obj]
    return obj


async def create(session: AsyncSession, identifiers: list[str], reason: str, actor: str,
                 team_id: uuid.UUID | None) -> ErasureRequest:
    identifiers = [i.strip() for i in identifiers if i and i.strip()]
    if not identifiers:
        from app.core.errors import ValidationFailedError

        raise ValidationFailedError("at least one subject identifier is needed")
    req = ErasureRequest(team_id=team_id, subject_hmacs=[crypto.value_hmac(i) for i in identifiers],
                         subject_types=sorted({t for i in identifiers for t in types_in(i)} or {"OTHER"}),
                         reason=reason, requested_by=actor, status="pending", required_approvals=2, votes=[])
    nonce, ct = crypto.encrypt_master(json.dumps(identifiers), "erasure")
    req.subjects_nonce, req.subjects_ciphertext = nonce, ct
    session.add(req)
    await session.flush()
    audit.enqueue(session, action="privacy.erasure_requested", team_id=team_id, object_type="erasure",
                  object_id=req.id, payload={"subject_types": req.subject_types, "reason": reason,
                                             "subjects": len(identifiers)})
    await session.commit()
    return req


async def vote(session: AsyncSession, request_id: uuid.UUID, actor: str, approve: bool,
               settings: Settings | None = None) -> ErasureRequest:
    """Part 11 vote rules: distinct voters, the requester can't vote (four eyes), one rejection rejects."""
    from app.governance.access import check_team
    from app.governance.principal import current

    req = await session.scalar(select(ErasureRequest).where(ErasureRequest.id == request_id).with_for_update())
    if req is None:
        raise NotFoundError(f"Erasure request {request_id} not found")
    check_team(req.team_id)
    if req.status != "pending":
        raise ConflictError(f"Erasure request is already {req.status}")
    p = current()
    voter = p.name if p.kind == "user" and p.name else ("admin_key" if p.kind == "admin_key" else actor)
    if voter == req.requested_by:
        raise ForbiddenError("Four-eyes rule: the requester cannot approve their own erasure request")
    if any(v["voter"] == voter for v in req.votes or []):
        raise ConflictError(f"{voter} already voted; it needs {req.required_approvals} different people")
    req.votes = [*(req.votes or []), {"voter": voter, "approve": approve, "at": utcnow().isoformat()}]
    audit.enqueue(session, action="privacy.erasure_vote", team_id=req.team_id, object_type="erasure",
                  object_id=req.id, payload={"voter": voter, "approve": approve, "votes": len(req.votes)})
    if not approve:
        req.status = "rejected"
        await session.commit()
        return req
    if sum(1 for v in req.votes if v["approve"]) >= req.required_approvals:
        req.status = "approved"
        await session.commit()
        return await execute(session, req.id, settings)
    await session.commit()
    return req


async def execute(session: AsyncSession, request_id: uuid.UUID, settings: Settings | None = None) -> ErasureRequest:
    settings = settings or get_settings()
    req = await session.scalar(select(ErasureRequest).where(ErasureRequest.id == request_id).with_for_update())
    if req is None or req.status != "approved":
        raise ConflictError("Only an approved erasure request can be executed")
    identifiers = json.loads(crypto.decrypt_master(req.subjects_nonce, req.subjects_ciphertext, "erasure"))
    pats = _variants(identifiers)
    team_filter = [Workflow.team_id == req.team_id] if req.team_id else []
    wfs = list(await session.scalars(select(Workflow.id).where(*team_filter)))
    affected: set[uuid.UUID] = set(await session.scalars(select(PiiToken.workflow_id).where(
        PiiToken.value_hmac.in_(req.subject_hmacs), PiiToken.workflow_id.in_(wfs))))
    counts = {"tasks_scrubbed": 0, "tool_calls_scrubbed": 0, "memories_deleted": 0, "events_scrubbed": 0,
              "files_deleted": 0, "workflows_shredded": 0, "stored_content_unreadable": 0}
    for t in await session.scalars(select(Task).where(Task.workflow_id.in_(wfs))):
        if _contains(t.output, pats) or _contains(t.input, pats):
            t.output, t.input = _scrub(t.output, pats), _scrub(t.input, pats)
            affected.add(t.workflow_id)
            counts["tasks_scrubbed"] += 1
    for c in await session.scalars(select(ToolCall).where(ToolCall.workflow_id.in_(wfs))):
        if _contains(c.arguments, pats) or _contains(c.result, pats):
            c.arguments, c.result = _scrub(c.arguments, pats), _scrub(c.result, pats)
            affected.add(c.workflow_id)
            counts["tool_calls_scrubbed"] += 1
    for e in await session.scalars(select(WorkflowEvent).where(WorkflowEvent.workflow_id.in_(wfs))):
        if _contains(e.payload, pats):
            e.payload = _scrub(e.payload, pats)
            affected.add(e.workflow_id)
            counts["events_scrubbed"] += 1
    for wid in wfs:  # encrypted stored content: decrypt + scan (only possible while the key exists)
        if wid in affected:
            continue
        rows = list(await session.scalars(select(StoredContent).where(StoredContent.workflow_id == wid)))
        if not rows:
            continue
        try:
            key = await crypto.data_key(session, wid, create=False)
        except crypto.ShreddedError:
            continue
        if any(_contains(crypto.decrypt(key, r.nonce, r.ciphertext, f"{wid}:{r.kind}"), pats) for r in rows):
            affected.add(wid)
    from app.privacy.retention import team_namespace_filter

    mem_where = [Memory.deleted_at.is_(None), *team_namespace_filter(req.team_id)]
    for m in list(await session.scalars(select(Memory).where(*mem_where))):
        if _contains(m.content, pats) or _contains(m.data, pats) or m.source_workflow_id in affected:
            await session.delete(m)
            counts["memories_deleted"] += 1
    for wid in affected:
        for d in (settings.workspaces_dir / str(wid), ):
            if d.exists():
                counts["files_deleted"] += sum(1 for _ in d.rglob("*") if _.is_file())
                shutil.rmtree(d, ignore_errors=True)
        for f in settings.outbox_dir.glob(f"*{wid}*") if settings.outbox_dir.exists() else []:
            f.unlink(missing_ok=True)
            counts["files_deleted"] += 1
        counts["stored_content_unreadable"] += len(list(await session.scalars(
            select(StoredContent.id).where(StoredContent.workflow_id == wid))))
        await session.execute(delete(PiiToken).where(PiiToken.workflow_id == wid))
        if (await session.execute(delete(WorkflowKey).where(WorkflowKey.workflow_id == wid))).rowcount:
            counts["workflows_shredded"] += 1
    published = sorted(str(p) for wid in affected for p in settings.publish_dir.glob(f"{wid}__*"))
    report = {**counts, "affected_workflows": sorted(str(w) for w in affected),
              "not_erased": {"published_files_manual": published,
                             "audit_chain": "kept: holds only hashes + redacted payloads (still verifiable)",
                             "checkpoint_anchors": "kept: hashes only"}}
    req.status, req.completed_at, req.report = "completed", utcnow(), report
    req.subjects_nonce = req.subjects_ciphertext = None  # the identifiers themselves are gone now
    audit.enqueue(session, action="privacy.erasure_completed", team_id=req.team_id, object_type="erasure",
                  object_id=req.id, payload={k: v for k, v in report.items() if k != "affected_workflows"}
                  | {"affected_workflows": len(affected)})
    await session.commit()
    return req


def to_dict(r: ErasureRequest) -> dict[str, Any]:
    return {"id": str(r.id), "status": r.status, "subject_types": r.subject_types, "reason": r.reason,
            "requested_by": r.requested_by, "required_approvals": r.required_approvals, "votes": r.votes,
            "report": r.report, "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None}
