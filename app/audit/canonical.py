"""Canonical JSON + the record hash (Part 12). Reimplementable by external tools; see docs/ARCHITECTURE.md.

canonical_json(obj):
  json.dumps(normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
  normalize: datetime -> UTC "YYYY-MM-DDTHH:MM:SS.ffffffZ"; date -> "YYYY-MM-DD"; Decimal/UUID -> str;
             tuple/set -> list (sets sorted); floats unchanged (Python repr); other types -> str.
record_hash(record, prev_hash) = sha256_hex( canonical_json(record without "hash") + prev_hash ), both UTF-8.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

GENESIS_PREV = "0" * 64
HASHED_FIELDS = ("chain_id", "seq", "at", "team_id", "actor_type", "actor_id", "auth_method", "ip", "action",
                 "object_type", "object_id", "decision", "payload", "payload_digest", "prev_hash")


def ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): normalize(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [normalize(v) for v in obj]
    if isinstance(obj, set | frozenset):
        return sorted(normalize(v) for v in obj)
    if isinstance(obj, datetime):
        return ts(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal | uuid.UUID):
        return str(obj)
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    return str(obj)


def canonical_json(obj: Any) -> str:
    return json.dumps(normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def digest(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def record_hash(record: dict[str, Any], prev_hash: str) -> str:
    body = {k: record.get(k) for k in HASHED_FIELDS}
    return sha256_hex(canonical_json(body) + prev_hash)
