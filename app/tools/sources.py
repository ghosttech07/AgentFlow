"""Source verification by code: a researcher may only cite pages it actually searched or opened in this task."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.llm.base import Source

CONFIDENCE_DOWN = {"high": "medium", "medium": "low", "low": "low"}


def normalize_url(url: str) -> str:
    """Comparable form: lowercase scheme/host, no www., no fragment, no tracking params, no trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not k.lower().startswith(("utm_", "fbclid", "gclid"))))
    path = parts.path.rstrip("/") or ""
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme.lower()
    return urlunsplit((scheme, host, path, query, ""))


def verify_sources(sources: list[Source], seen_urls: set[str]) -> tuple[list[Source], list[Source]]:
    """(verified, removed): a source is kept only if its URL was returned by web_search or fetched."""
    seen = {normalize_url(u) for u in seen_urls}
    verified: list[Source] = []
    removed: list[Source] = []
    for s in sources:
        (verified if normalize_url(s.url) in seen else removed).append(s)
    return verified, removed


def lower_confidence(level: str) -> str:
    return CONFIDENCE_DOWN.get(level, "low")
