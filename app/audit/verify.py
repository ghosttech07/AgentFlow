"""Audit chain verification (Part 12). Recomputes every hash, checks seq continuity, links, checkpoints + signatures
and (optionally) the exported anchor files. Reports OK up to seq N, or the FIRST break with its kind:

  deleted               a seq is missing
  modified              a record's content no longer matches its own hash
  inserted              prev_hash matches no earlier record (a foreign record was added)
  reordered             prev_hash matches a different earlier record than the previous one
  checkpoint_mismatch   a checkpoint's head (or the anchor file's copy) differs from the chain
  bad_signature         a checkpoint signature does not verify with its key_id's public key
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.canonical import GENESIS_PREV, record_hash
from app.audit.checkpoints import statement
from app.audit.keys import verify as verify_sig
from app.models.audit import AuditCheckpoint, AuditLog


@dataclass
class VerifyResult:
    chain_id: str
    ok: bool
    verified_seq: int
    records: int
    checkpoints: int
    first_broken_seq: int | None = None
    kind: str | None = None
    detail: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def row_dict(r: AuditLog | dict[str, Any]) -> dict[str, Any]:
    if isinstance(r, dict):
        return r
    return {c: getattr(r, c) for c in ("chain_id", "seq", "at", "team_id", "actor_type", "actor_id", "auth_method",
                                       "ip", "action", "object_type", "object_id", "decision", "payload",
                                       "payload_digest", "prev_hash", "hash")}


def verify_records(chain_id: str, records: list[dict[str, Any]], checkpoints: list[dict[str, Any]],
                   key_dirs: list[Path], *, start_prev: str | None = None, anchors: list[dict[str, Any]] | None = None,
                   ) -> VerifyResult:
    """Pure verification over plain dicts (shared by the server, scripts/verify_audit.py and verify_evidence.py).

    `start_prev`: the prev_hash expected for the first record (a segment that doesn't start at genesis)."""
    records = sorted(records, key=lambda r: r["seq"])
    res = VerifyResult(chain_id=chain_id, ok=True, verified_seq=-1, records=len(records), checkpoints=len(checkpoints))

    def fail(seq: int, kind: str, detail: str) -> VerifyResult:
        res.ok, res.first_broken_seq, res.kind, res.detail = False, seq, kind, detail
        return res

    all_hashes = {r["hash"]: r["seq"] for r in records}  # to tell a reorder from a foreign insert
    hashes: dict[int, str] = {}
    prev = start_prev if start_prev is not None else GENESIS_PREV
    expected_seq = records[0]["seq"] if records and start_prev is not None else 0
    for r in records:
        if r["seq"] != expected_seq:
            if r["seq"] > expected_seq:
                return fail(expected_seq, "deleted", f"seq {expected_seq} is missing (next present: {r['seq']})")
            return fail(r["seq"], "inserted", f"duplicate or out-of-order seq {r['seq']}")
        if r["prev_hash"] != prev:
            if r["prev_hash"] in all_hashes:
                return fail(r["seq"], "reordered",
                            f"prev_hash points at the record now at seq {all_hashes[r['prev_hash']]}, "
                            f"not seq {r['seq'] - 1}")
            if record_hash(r, r["prev_hash"]) == r["hash"]:
                return fail(r["seq"], "inserted", "a self-consistent record whose prev_hash matches no earlier record")
            return fail(r["seq"], "modified", "prev_hash changed")
        if record_hash(r, prev) != r["hash"]:
            return fail(r["seq"], "modified", "the record's content does not match its hash")
        hashes[r["seq"]] = r["hash"]
        prev = r["hash"]
        res.verified_seq = r["seq"]
        expected_seq += 1
    for cp in sorted(checkpoints, key=lambda c: c["seq"]):
        if cp["seq"] not in hashes:
            if cp["seq"] > res.verified_seq:
                return fail(cp["seq"], "deleted", f"checkpoint at seq {cp['seq']} is beyond the last record")
            continue  # outside this segment
        if hashes[cp["seq"]] != cp["head_hash"]:
            return fail(cp["seq"], "checkpoint_mismatch", "the chain's hash at this seq differs from the signed head")
        if not verify_sig(cp["key_id"], statement(cp["chain_id"], cp["seq"], cp["head_hash"], _at(cp["at"]),
                                                  cp["key_id"]), cp["signature"], key_dirs):
            return fail(cp["seq"], "bad_signature", f"checkpoint signature invalid for key {cp['key_id']}")
    if anchors is not None:
        db = {c["seq"]: c for c in checkpoints}
        for a in anchors:
            c = db.get(a["seq"])
            if c is None or c["head_hash"] != a["head_hash"] or c["signature"] != a["signature"]:
                return fail(a["seq"], "checkpoint_mismatch", "the exported anchor file differs from the DB checkpoint")
    return res


def _at(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def read_anchors(anchor_dir: Path | None, chain_id: str) -> list[dict[str, Any]] | None:
    if anchor_dir is None:
        return None
    path = Path(anchor_dir) / f"{chain_id}.jsonl"
    if not path.exists():
        return None
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def verify_chain(session: AsyncSession, chain_id: str, key_dirs: list[Path],
                       anchor_dir: Path | None = None) -> VerifyResult:
    rows = [row_dict(r) for r in await session.scalars(select(AuditLog).where(AuditLog.chain_id == chain_id)
                                                        .order_by(AuditLog.seq))]
    cps = [{"chain_id": c.chain_id, "seq": c.seq, "head_hash": c.head_hash, "at": c.at, "key_id": c.key_id,
            "signature": c.signature}
           for c in await session.scalars(select(AuditCheckpoint).where(AuditCheckpoint.chain_id == chain_id))]
    return verify_records(chain_id, rows, cps, key_dirs, anchors=read_anchors(anchor_dir, chain_id))


async def chains(session: AsyncSession) -> list[str]:
    from app.models.audit import AuditChainHead

    return list(await session.scalars(select(AuditChainHead.chain_id).order_by(AuditChainHead.chain_id)))
