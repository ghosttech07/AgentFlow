"""Signed checkpoints (Part 12): "chain X had head H at seq N", signed with Ed25519 and copied OUTSIDE the DB.

Created by the engine tick every AUDIT_CHECKPOINT_EVERY_RECORDS records or AUDIT_CHECKPOINT_MINUTES (if anything
new). Exported to: outputs/audit-anchors/<chain>.jsonl (append), a webhook, and/or a git repo commit.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.canonical import canonical_json, ts
from app.audit.keys import sign
from app.config import Settings, get_settings
from app.db.base import utcnow
from app.models.audit import AuditChainHead, AuditCheckpoint

logger = logging.getLogger(__name__)


def statement(chain_id: str, seq: int, head_hash: str, at: Any, key_id: str) -> bytes:
    """The exact bytes that are signed (document this for external verifiers)."""
    return canonical_json({"chain_id": chain_id, "seq": seq, "head_hash": head_hash, "at": ts(at),
                           "key_id": key_id}).encode()


async def create(session: AsyncSession, head: AuditChainHead) -> AuditCheckpoint:
    at = utcnow()
    _, kid = sign(b"")  # resolve the key id first (the statement contains it)
    sig, kid = sign(statement(head.chain_id, head.seq, head.head_hash, at, kid))
    cp = AuditCheckpoint(chain_id=head.chain_id, seq=head.seq, head_hash=head.head_hash, at=at, key_id=kid,
                         signature=sig)
    session.add(cp)
    head.last_checkpoint_seq, head.last_checkpoint_at = head.seq, at
    return cp


def as_dict(cp: AuditCheckpoint) -> dict[str, Any]:
    return {"chain_id": cp.chain_id, "seq": cp.seq, "head_hash": cp.head_hash, "at": ts(cp.at), "key_id": cp.key_id,
            "signature": cp.signature}


async def export(cp: AuditCheckpoint, settings: Settings) -> None:
    """Best effort: a failed export is logged (the DB copy exists); the next checkpoint exports again."""
    line = json.dumps(as_dict(cp), sort_keys=True)
    if settings.audit_anchor_dir:
        try:
            settings.audit_anchor_dir.mkdir(parents=True, exist_ok=True)
            with open(settings.audit_anchor_dir / f"{cp.chain_id}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logger.exception("audit.anchor_file_failed chain=%s", cp.chain_id)
    if settings.audit_anchor_webhook_url:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(settings.audit_anchor_webhook_url, json=as_dict(cp))
        except httpx.HTTPError:
            logger.warning("audit.anchor_webhook_failed chain=%s", cp.chain_id)
    if settings.audit_anchor_git_repo:
        repo = settings.audit_anchor_git_repo
        try:
            path = repo / f"{cp.chain_id}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            subprocess.run(["git", "-C", str(repo), "add", path.name], check=True, capture_output=True, timeout=20)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", f"audit checkpoint {cp.chain_id} seq {cp.seq}"],
                           check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            logger.warning("audit.anchor_git_failed chain=%s", cp.chain_id)


async def maybe_checkpoint(session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None,
                           force: bool = False) -> list[AuditCheckpoint]:
    settings = settings or get_settings()
    made: list[AuditCheckpoint] = []
    async with session_factory() as s:
        heads = list(await s.scalars(select(AuditChainHead).with_for_update(skip_locked=True)))
        now = utcnow()
        for h in heads:
            new = h.seq - h.last_checkpoint_seq
            due_time = h.last_checkpoint_at is None or now - h.last_checkpoint_at >= timedelta(
                minutes=settings.audit_checkpoint_minutes)
            if new > 0 and (force or new >= settings.audit_checkpoint_every_records or due_time):
                made.append(await create(s, h))
        await s.commit()
    for cp in made:
        await export(cp, settings)
    return made
