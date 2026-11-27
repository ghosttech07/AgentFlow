"""Background audit jobs (Part 12), run from the engine loop: checkpoints + incremental integrity verification.

Incremental: each run verifies the records after the last verified seq (starting from that record's hash) plus the
checkpoints in that range; a failure marks the chain, records `audit.integrity_failed` on the system chain and
posts AUDIT_INTEGRITY_WEBHOOK_URL. A full verification is always available (GET /audit/verify, verify_audit.py).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import chain as audit
from app.audit.checkpoints import maybe_checkpoint
from app.audit.verify import VerifyResult, read_anchors, row_dict, verify_records
from app.config import Settings, get_settings
from app.db.base import utcnow
from app.models.audit import AuditChainHead, AuditCheckpoint, AuditLog

logger = logging.getLogger(__name__)


async def verify_incremental(session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None,
                             ) -> list[VerifyResult]:
    settings = settings or get_settings()
    results = []
    async with session_factory() as s:
        heads = list(await s.scalars(select(AuditChainHead)))
    for h in heads:
        async with session_factory() as s:
            start = max(h.verified_seq, -1)
            prev = None
            if start >= 0:
                prev = await s.scalar(select(AuditLog.hash).where(AuditLog.chain_id == h.chain_id,
                                                                  AuditLog.seq == start))
            rows = [row_dict(r) for r in await s.scalars(select(AuditLog).where(
                AuditLog.chain_id == h.chain_id, AuditLog.seq > start).order_by(AuditLog.seq))]
            cps = [{"chain_id": c.chain_id, "seq": c.seq, "head_hash": c.head_hash, "at": c.at, "key_id": c.key_id,
                    "signature": c.signature} for c in await s.scalars(select(AuditCheckpoint).where(
                        AuditCheckpoint.chain_id == h.chain_id, AuditCheckpoint.seq > start))]
            if not rows:
                continue
            r = verify_records(h.chain_id, rows, cps, settings.audit_public_keys_dirs,
                               start_prev=prev if start >= 0 else None,
                               anchors=[a for a in (read_anchors(settings.audit_anchor_dir, h.chain_id) or [])
                                        if a["seq"] > start] or None)
            head = await s.get(AuditChainHead, h.chain_id, with_for_update=True)
            head.verified_at = utcnow()
            if r.ok:
                head.verified_seq, head.integrity_ok, head.integrity_error = r.verified_seq, True, None
            else:
                head.integrity_ok = False
                head.integrity_error = f"seq {r.first_broken_seq}: {r.kind} ({r.detail})"
                await audit.append_now(s, action="audit.integrity_failed", object_type="chain", object_id=h.chain_id,
                                       payload=r.to_dict(), actor={"actor_type": "system", "actor_id": "verifier",
                                                                   "auth_method": None, "ip": None})
                logger.error("audit.integrity_failed chain=%s seq=%s kind=%s", h.chain_id, r.first_broken_seq, r.kind)
                if settings.audit_integrity_webhook_url:
                    try:
                        async with httpx.AsyncClient(timeout=5) as c:
                            await c.post(settings.audit_integrity_webhook_url, json={"alert": "audit.integrity_failed",
                                                                                     **r.to_dict()})
                    except httpx.HTTPError:
                        logger.warning("audit.integrity_webhook_failed")
            await s.commit()
            results.append(r)
    return results


class AuditMaintenance:
    """Called every engine tick; does real work only when its intervals are due."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sf, self.settings = session_factory, settings
        self._next_cp = utcnow()
        self._next_verify = utcnow() + timedelta(minutes=1)
        self._next_retention = utcnow() + timedelta(minutes=5)

    async def run(self) -> None:
        now = utcnow()
        try:
            if now >= self._next_cp:
                self._next_cp = now + timedelta(seconds=30)
                await maybe_checkpoint(self.sf, self.settings)
            if now >= self._next_verify:
                self._next_verify = now + timedelta(minutes=self.settings.audit_verify_minutes)
                await verify_incremental(self.sf, self.settings)
            if now >= self._next_retention:
                self._next_retention = now + timedelta(hours=self.settings.retention_job_hours)
                from app.privacy import retention

                await retention.apply_all(self.sf, self.settings)
        except Exception:  # never break the scheduler loop; the next tick retries
            logger.exception("audit.maintenance_failed")
