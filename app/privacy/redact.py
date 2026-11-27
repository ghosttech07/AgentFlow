"""Masking for places that must never hold raw PII or secrets: audit payloads, logs, exports (Part 12)."""

from __future__ import annotations

import logging
from typing import Any

from app.privacy.detectors import detect

MAX_SCAN = 200_000  # very long strings are truncated in redacted copies, never scanned partially and kept raw


def mask_text(text: str, types: set[str] | None = None) -> str:
    if not text:
        return text
    if len(text) > MAX_SCAN:
        text = text[:MAX_SCAN] + " [truncated]"
    out, last = [], 0
    for f in detect(text):
        if types is not None and f.type not in types:
            continue
        out.append(text[last:f.start])
        out.append(f"[{f.type}]")
        last = f.end
    out.append(text[last:])
    return "".join(out)


def redact_obj(obj: Any) -> Any:
    """A copy with every string masked (dict keys kept)."""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_obj(v) for v in obj]
    return obj


class RedactingFilter(logging.Filter):
    """Secrets and PII are masked in every log line, regardless of config (Part 12)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not kill logging
            return True
        masked = mask_text(msg)
        if masked != msg:
            record.msg, record.args = masked, None
        return True
