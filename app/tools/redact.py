"""Redaction for anything that reaches the journal, events or logs: secrets never leave the tool."""

from __future__ import annotations

from typing import Any

MASK = "***"
SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie",
                               "set-cookie", "x-auth-token"})


def redact(value: Any, secrets: list[str]) -> Any:
    """Deep copy with every known secret value replaced and sensitive header values masked."""
    secrets = [s for s in secrets if s and len(s) >= 4]
    if isinstance(value, dict):
        return {k: (MASK if isinstance(k, str) and k.lower() in SENSITIVE_HEADERS else redact(v, secrets))
                for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, MASK)
        return value
    return value
