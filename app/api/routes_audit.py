"""Audit endpoints (Part 12): search, integrity, checkpoints, the team report. Reading the audit log is audited."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_permission
from app.audit import chain as audit
from app.audit.canonical import ts
from app.audit.verify import verify_chain
from app.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.session import get_session
from app.governance.principal import Principal
from app.models.audit import AuditChainHead, AuditCheckpoint, AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


def _chains_for(p: Principal) -> list[str] | None:
    """None = every chain (global admin); otherwise only the caller's team chain."""
    return None if p.is_global else [audit.chain_for(p.team_id)]


def _check_chain(p: Principal, chain: str) -> None:
    allowed = _chains_for(p)
    if allowed is not None and chain not in allowed:
        raise NotFoundError(f"Chain {chain} not found")


def _row(r: AuditLog) -> dict[str, Any]:
    return {"chain_id": r.chain_id, "seq": r.seq, "at": ts(r.at), "team_id": r.team_id, "actor_type": r.actor_type,
            "actor_id": r.actor_id, "auth_method": r.auth_method, "ip": r.ip, "action": r.action,
            "object_type": r.object_type, "object_id": r.object_id, "decision": r.decision, "payload": r.payload,
            "payload_digest": r.payload_digest, "prev_hash": r.prev_hash, "hash": r.hash}


@router.get("")
async def search(actor: str | None = None, action: str | None = None, object_id: str | None = None,
                 effect: str | None = None, since: datetime | None = Query(default=None, alias="from"),
                 until: datetime | None = Query(default=None, alias="to"), chain: str | None = None,
                 limit: int = Query(default=100, ge=1, le=1000),
                 p: Principal = Depends(require_permission("audit.view")),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Audit records, newest first, filtered (team-scoped). Payloads are stored redacted."""
    stmt = select(AuditLog)
    allowed = _chains_for(p)
    if allowed is not None:
        stmt = stmt.where(AuditLog.chain_id.in_(allowed))
    if chain:
        _check_chain(p, chain)
        stmt = stmt.where(AuditLog.chain_id == chain)
    if actor:
        stmt = stmt.where(AuditLog.actor_id == actor)
    if action:
        stmt = stmt.where(AuditLog.action.like(action.replace("*", "%")))
    if object_id:
        stmt = stmt.where(AuditLog.object_id == object_id)
    if effect:
        stmt = stmt.where(AuditLog.decision["effect"].astext == effect)
    if since:
        stmt = stmt.where(AuditLog.at >= since)
    if until:
        stmt = stmt.where(AuditLog.at <= until)
    rows = list(await session.scalars(stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit)))
    await audit.append_now(session, action="audit.read", team_id=p.team_id, object_type="audit",
                           payload={"filters": {"actor": actor, "action": action, "object_id": object_id,
                                                "effect": effect, "chain": chain}, "returned": len(rows)})
    await session.commit()
    return {"items": [_row(r) for r in rows]}


@router.get("/chains")
async def chains(p: Principal = Depends(require_permission("audit.view")),
                 session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    """Integrity status per chain (from the background verifier) + the latest checkpoint."""
    stmt = select(AuditChainHead).order_by(AuditChainHead.chain_id)
    allowed = _chains_for(p)
    if allowed is not None:
        stmt = stmt.where(AuditChainHead.chain_id.in_(allowed))
    out = []
    for h in await session.scalars(stmt):
        cp = await session.scalar(select(AuditCheckpoint).where(AuditCheckpoint.chain_id == h.chain_id)
                                  .order_by(AuditCheckpoint.seq.desc()))
        out.append({"chain_id": h.chain_id, "seq": h.seq, "head_hash": h.head_hash, "verified_seq": h.verified_seq,
                    "verified_at": ts(h.verified_at) if h.verified_at else None, "integrity_ok": h.integrity_ok,
                    "integrity_error": h.integrity_error,
                    "last_checkpoint": {"seq": cp.seq, "head_hash": cp.head_hash, "at": ts(cp.at), "key_id": cp.key_id}
                    if cp else None})
    return out


@router.get("/checkpoints")
async def list_checkpoints(chain: str, p: Principal = Depends(require_permission("audit.view")),
                           session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    _check_chain(p, chain)
    rows = await session.scalars(select(AuditCheckpoint).where(AuditCheckpoint.chain_id == chain)
                                 .order_by(AuditCheckpoint.seq.desc()).limit(200))
    return [{"seq": c.seq, "head_hash": c.head_hash, "at": ts(c.at), "key_id": c.key_id, "signature": c.signature}
            for c in rows]


@router.get("/verify")
async def verify(chain: str | None = None, p: Principal = Depends(require_permission("audit.view")),
                 settings: Settings = Depends(get_settings),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Full re-verification now: OK up to seq N, or the first broken seq and its kind."""
    from app.audit.verify import chains as all_chains

    targets = [chain] if chain else (_chains_for(p) or await all_chains(session))
    for c in targets:
        _check_chain(p, c)
    results = [(await verify_chain(session, c, settings.audit_public_keys_dirs, settings.audit_anchor_dir)).to_dict()
               for c in targets]
    await audit.append_now(session, action="audit.verified", team_id=p.team_id, object_type="audit",
                           payload={"chains": targets, "ok": all(r["ok"] for r in results)})
    await session.commit()
    return {"ok": all(r["ok"] for r in results), "chains": results}


@router.get("/report")
async def report(since: datetime | None = Query(default=None, alias="from"),
                 until: datetime | None = Query(default=None, alias="to"),
                 p: Principal = Depends(require_permission("audit.view")),
                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Team compliance report: approvals, denials, policy changes, access events, integrity."""
    where = []
    allowed = _chains_for(p)
    if allowed is not None:
        where.append(AuditLog.chain_id.in_(allowed))
    if since:
        where.append(AuditLog.at >= since)
    if until:
        where.append(AuditLog.at <= until)
    counts = dict((await session.execute(select(AuditLog.action, func.count()).where(*where)
                                         .group_by(AuditLog.action))).all())

    def pick(*prefixes: str) -> dict[str, int]:
        return {k: v for k, v in sorted(counts.items()) if k.startswith(prefixes)}

    denials = await session.scalar(select(func.count()).select_from(AuditLog).where(
        *where, AuditLog.decision["effect"].astext.in_(["deny", "block"])))
    heads = await chains(p, session)
    await audit.append_now(session, action="audit.report", team_id=p.team_id, object_type="audit",
                           payload={"from": since.isoformat() if since else None,
                                    "to": until.isoformat() if until else None})
    await session.commit()
    return {"approvals": pick("approval."), "denials": int(denials or 0),
            "policy_changes": pick("policy.", "proposal."),
            "access": pick("auth.", "user.", "token.", "privacy.reveal", "audit.export", "privacy.erasure",
                           "evidence."),
            "integrity": [{"chain_id": h["chain_id"], "ok": h["integrity_ok"], "verified_seq": h["verified_seq"]}
                          for h in heads],
            "records": sum(counts.values())}
