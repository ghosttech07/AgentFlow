"""Live web research for agents: search, read the top pages, return numbered sources.

Free by default. Provider order with WEB_SEARCH_PROVIDER=auto:
  1. Tavily        if TAVILY_API_KEY is set (free tier; built for AI agents, returns page text)
  2. ddgs          no key: metasearch over the engines in WEB_SEARCH_BACKENDS (bing, yahoo, google)
Gemini's own Google Search grounding is separate (ENABLE_SEARCH_GROUNDING, needs billing); when it
is on, the researcher uses it instead of this module.

Pages are fetched in parallel with a per-page timeout and reduced to their main text (trafilatura).
Failures never raise: research continues with whatever was found (possibly nothing).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")


@dataclass
class WebSource:
    n: int
    title: str
    url: str
    snippet: str
    content: str = ""  # main page text (truncated); empty if the page could not be read

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.removeprefix("www.")


@dataclass
class WebContext:
    queries: list[str]
    sources: list[WebSource] = field(default_factory=list)
    provider: str = "none"
    search_ms: int = 0
    fetch_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def as_prompt(self, retrieved_on: str) -> str:
        if not self.sources:
            return (f"## WEB SOURCES (searched {retrieved_on})\nThe web search returned nothing usable. "
                    "Say clearly that current data could not be retrieved.\n")
        blocks = [f"[{s.n}] {s.title}\nURL: {s.url}\n{s.content or s.snippet}" for s in self.sources]
        return (f"## WEB SOURCES (retrieved {retrieved_on}; cite them as [n])\n\n"
                + "\n\n---\n\n".join(blocks) + "\n")


def provider_name(settings: Settings) -> str:
    choice = settings.web_search_provider
    if choice == "auto":
        return "tavily" if settings.tavily_api_key else "ddgs"
    return choice


async def _search_ddgs(query: str, settings: Settings) -> list[dict[str, str]]:
    from ddgs import DDGS  # imported lazily: optional at import time, slow to import

    def run() -> list[dict[str, str]]:
        # Free engines fail intermittently ("no results", throttling): try them one by one, in order.
        error: Exception | None = None
        for backend in [b.strip() for b in settings.web_search_backends.split(",") if b.strip()]:
            try:
                rows = DDGS(timeout=10).text(query, max_results=settings.web_max_results, backend=backend)
            except Exception as exc:
                error = exc
                continue
            found = [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                     for r in rows if r.get("href")]
            if found:
                return found
        if error:
            raise error
        return []

    return await asyncio.to_thread(run)  # ddgs is synchronous


async def _search_tavily(query: str, settings: Settings) -> list[dict[str, str]]:
    assert settings.tavily_api_key is not None
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post("https://api.tavily.com/search", json={
            "api_key": settings.tavily_api_key.get_secret_value(), "query": query,
            "max_results": settings.web_max_results, "search_depth": "basic", "include_raw_content": True,
        })
        resp.raise_for_status()
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", ""),
             "content": (r.get("raw_content") or "")} for r in resp.json().get("results", []) if r.get("url")]


async def _fetch_text(client: httpx.AsyncClient, url: str, settings: Settings) -> str:
    import trafilatura

    try:
        resp = await client.get(url, timeout=settings.web_fetch_timeout_seconds)
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
            return ""
        # Raw bytes: trafilatura detects the real charset (httpx guesses wrong on some pages: "â‚¹" for "₹").
        text = await asyncio.to_thread(trafilatura.extract, resp.content, include_comments=False) or ""
    except Exception as exc:  # unreachable / slow / blocked pages are simply skipped
        logger.debug("web.fetch_failed url=%s error=%s", url, type(exc).__name__)
        return ""
    return text


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


async def gather(queries: list[str], settings: Settings) -> WebContext:
    """Search every query, de-duplicate URLs, read the top pages. Never raises."""
    queries = [q for q in dict.fromkeys(q.strip()[:300] for q in queries) if q]
    ctx = WebContext(queries=queries, provider=provider_name(settings))
    if ctx.provider == "none" or not queries:
        return ctx
    search = _search_tavily if ctx.provider == "tavily" else _search_ddgs

    started = time.perf_counter()
    results = await asyncio.gather(*(search(q, settings) for q in queries), return_exceptions=True)
    ctx.search_ms = int((time.perf_counter() - started) * 1000)
    seen: dict[str, dict[str, str]] = {}
    for q, rows in zip(queries, results, strict=True):
        if isinstance(rows, BaseException):
            ctx.errors.append(f"search failed for {q!r}: {type(rows).__name__}")
            logger.warning("web.search_failed provider=%s error=%s", ctx.provider, type(rows).__name__)
            continue
        for row in rows:
            seen.setdefault(row["url"].split("#")[0], row)
    # Interleave would need ranks per query; keep engine order (best first) and cap the page count.
    candidates = list(seen.values())[: settings.web_max_pages * 2]

    started = time.perf_counter()
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        texts = await asyncio.gather(*(
            asyncio.sleep(0, result=row["content"]) if row.get("content") else _fetch_text(client, row["url"], settings)
            for row in candidates
        ))
    ctx.fetch_ms = int((time.perf_counter() - started) * 1000)

    # Prefer pages we could read; fall back to snippets so the model still sees something.
    ranked = sorted(zip(candidates, texts, strict=True), key=lambda pair: not pair[1])
    for row, text in ranked[: settings.web_max_pages]:
        ctx.sources.append(WebSource(n=len(ctx.sources) + 1, title=row["title"] or row["url"], url=row["url"],
                                     snippet=_clip(row.get("snippet", ""), 400),
                                     content=_clip(text, settings.web_page_chars)))
    logger.info("web.gathered provider=%s queries=%d sources=%d read=%d search_ms=%d fetch_ms=%d",
                ctx.provider, len(queries), len(ctx.sources), sum(bool(s.content) for s in ctx.sources),
                ctx.search_ms, ctx.fetch_ms)
    return ctx
