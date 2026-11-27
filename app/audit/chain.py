"""Appending to the audit chain (Part 12) in the SAME transaction as the state change.

Every workflow event (`core.events.record_event`) and every non-workflow action (`enqueue`) becomes a pending record
on the session; a `before_flush` listener appends them in order. For each record:
  pg_advisory_xact_lock(chain) -> read the chain head (genesis on first use) -> seq+1, prev_hash = head ->
  hash = sha256(canonical(record) + prev_hash) -> insert row, advance the head.
The advisory lock serializes appends per chain across processes (no forks, gapless seq); it is held until the
transaction ends, and a rollback removes the audit rows together with the state change.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.audit.canonical import GENESIS_PREV, digest, record_hash
from app.db.base import utcnow
from app.models.audit import AuditChainHead, AuditLog

logger = logging.getLogger(__name__)
SYSTEM_CHAIN = "system"
PENDING = "audit_pending"


def chain_for(team_id: uuid.UUID | str | None) -> str:
    return str(team_id) if team_id else SYSTEM_CHAIN


def _actor() -> dict[str, Any]:
    from app.governance.principal import current

    p = current()
    if p.kind == "user":
        return {"actor_type": "token" if p.auth_method == "api_key" else "user", "actor_id": p.name,
                "auth_method": p.auth_method, "ip": p.ip}
    if p.kind == "admin_key":
        return {"actor_type": "token", "actor_id": "admin_key", "auth_method": "admin_key", "ip": p.ip}
    if p.kind == "system":
        return {"actor_type": "system", "actor_id": "engine", "auth_method": None, "ip": None}
    return {"actor_type": "user", "actor_id": None, "auth_method": "none", "ip": p.ip}  # open / anonymous


def enqueue(session: AsyncSession | Session, *, action: str, team_id: uuid.UUID | str | None = None,
            object_type: str | None = None, object_id: Any = None, payload: dict[str, Any] | None = None,
            decision: dict[str, Any] | None = None, actor: dict[str, Any] | None = None,
            workflow_id: uuid.UUID | None = None) -> None:
    """Queue one audit record; it is written by the next flush of this session (same transaction).

    `workflow_id` instead of `team_id`: the chain is the workflow's team (resolved at flush time)."""
    sync = session.sync_session if isinstance(session, AsyncSession) else session
    sync.info.setdefault(PENDING, []).append({
        "action": action, "team_id": str(team_id) if team_id else None, "workflow_id": workflow_id,
        "object_type": object_type, "object_id": str(object_id) if object_id is not None else None,
        "payload": payload or {}, "decision": decision, **(actor or _actor())})


async def append_now(session: AsyncSession, **kwargs: Any) -> None:
    """enqueue + flush now: for actions that change nothing else (a read, an export, a login)."""
    enqueue(session, **kwargs)
    await session.run_sync(_drain)


def _team_of_workflow(s: Session, workflow_id: uuid.UUID) -> str | None:
    from app.models.workflow import Workflow

    for obj in s.new:  # a workflow created in this same flush
        if isinstance(obj, Workflow) and obj.id == workflow_id:
            return str(obj.team_id) if obj.team_id else None
    with s.no_autoflush:
        wf = s.get(Workflow, workflow_id)  # identity map first, else one SELECT
    return str(wf.team_id) if wf is not None and wf.team_id else None


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.privacy.redact import redact_obj

        return redact_obj(payload)
    except ImportError:  # pragma: no cover
        return payload


def _head(s: Session, chain: str) -> AuditChainHead:
    cache: dict[str, AuditChainHead] = s.info.setdefault("audit_heads", {})
    if chain in cache:
        return cache[chain]
    with s.no_autoflush:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "audit:" + chain})
        head = s.execute(select(AuditChainHead).where(AuditChainHead.chain_id == chain)
                         .with_for_update().execution_options(populate_existing=True)).scalar()
    if head is None:  # genesis
        at = utcnow()
        rec = {"chain_id": chain, "seq": 0, "at": at, "team_id": None if chain == SYSTEM_CHAIN else chain,
               "actor_type": "system", "actor_id": "engine", "auth_method": None, "ip": None,
               "action": "chain.genesis", "object_type": "chain", "object_id": chain, "decision": None,
               "payload": {"chain_id": chain}, "payload_digest": digest({"chain_id": chain}), "prev_hash": GENESIS_PREV}
        h = record_hash(rec, GENESIS_PREV)
        s.add(AuditLog(**rec, hash=h))
        head = AuditChainHead(chain_id=chain, seq=0, head_hash=h, last_checkpoint_seq=-1)
        s.add(head)
    cache[chain] = head
    return head


def _append_one(s: Session, item: dict[str, Any]) -> None:
    team = item["team_id"]
    if team is None and item.get("workflow_id") is not None:
        team = _team_of_workflow(s, item["workflow_id"])
    chain = chain_for(team)
    head = _head(s, chain)
    original = item["payload"]
    rec = {"chain_id": chain, "seq": head.seq + 1, "at": utcnow(), "team_id": team,
           "actor_type": item["actor_type"], "actor_id": item["actor_id"], "auth_method": item["auth_method"],
           "ip": item["ip"], "action": item["action"][:64], "object_type": item["object_type"],
           "object_id": item["object_id"], "decision": item["decision"], "payload": _redact(original),
           "payload_digest": digest(original), "prev_hash": head.head_hash}
    h = record_hash(rec, head.head_hash)
    s.add(AuditLog(**rec, hash=h))
    head.seq, head.head_hash = rec["seq"], h


def _drain(s: Session) -> None:
    pending = s.info.pop(PENDING, None)
    if not pending:
        return
    for item in pending:
        _append_one(s, item)
    s.flush()


@event.listens_for(Session, "before_flush")
def _before_flush(s: Session, flush_context: Any, instances: Any) -> None:
    pending = s.info.pop(PENDING, None)
    if pending:
        for item in pending:
            _append_one(s, item)


@event.listens_for(Session, "before_commit")
def _before_commit(s: Session) -> None:
    """A commit with only Core statements (UPDATE/DELETE, no ORM changes) skips before_flush: drain here too."""
    if s.info.get(PENDING):
        _drain(s)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _reset(s: Session) -> None:
    s.info.pop("audit_heads", None)  # the advisory lock ended with the transaction
    s.info.pop(PENDING, None)  # a rollback discards the records together with the state change
