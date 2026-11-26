"""/providers and /metrics/summary (Part 8). Built from the router's live state and llm_calls rows.
Keys are only ever reported as `key_present: true/false`."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import quota
from app.llm.providers.registry import PROVIDERS
from app.llm.router import ModelRouter
from app.llm.routes import split_entry
from app.models.usage import LLMCall


async def providers_status(router: ModelRouter) -> dict[str, Any]:
    await router.ensure_started()
    assert router.routes is not None and router.providers is not None
    s = router.settings
    keys = {"gemini": s.gemini_api_key is not None, "anthropic": s.anthropic_api_key is not None,
            "openai": s.openai_api_key is not None, "ollama": None, "fake": None}  # ollama/fake need no key
    entries: dict[str, list[str]] = defaultdict(list)
    for route in router.routes.routes.values():
        for entry in route:
            provider = split_entry(entry)[0]
            if entry not in entries[provider]:
                entries[provider].append(entry)
    out = []
    for name in sorted(set(PROVIDERS) | set(router.providers) | set(entries)):
        rows = []
        for entry in entries.get(name, []):
            spec = router.routes.quotas.get(entry)
            usage = (await quota.usage_now(router.session_factory, entry, spec, s)
                     if router.session_factory is not None else None)
            rows.append({"entry": entry, "model": split_entry(entry)[1], "circuit": router.breaker.state(entry),
                         "quota": usage})
        out.append({"name": name, "configured": name in router.providers, "key_present": keys.get(name),
                    "entries": rows})
    return {"providers": out, "routes": router.routes.routes}


def _stats(calls: int, errors: int, latency_sum: float, cost: float | None, tokens: int) -> dict[str, Any]:
    return {"calls": calls, "errors": errors, "error_rate": round(errors / calls, 3) if calls else 0.0,
            "avg_latency_ms": round(latency_sum / calls) if calls else None,
            "cost_usd": round(cost, 6) if cost is not None else None, "tokens": tokens}


async def metrics_summary(session: AsyncSession) -> dict[str, Any]:
    failed = func.count().filter(LLMCall.outcome == "failed")
    rows = (await session.execute(
        select(LLMCall.provider, LLMCall.model, func.count(), failed,
               func.coalesce(func.sum(LLMCall.latency_ms), 0), func.coalesce(func.sum(LLMCall.cost_usd), 0),
               func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0))
        .group_by(LLMCall.provider, LLMCall.model)
    )).all()
    totals = (await session.execute(select(
        func.count(), failed, func.coalesce(func.sum(LLMCall.cost_usd), 0),
        func.coalesce(func.sum(LLMCall.input_tokens), 0), func.coalesce(func.sum(LLMCall.output_tokens), 0),
        func.count().filter(LLMCall.outcome == "succeeded", LLMCall.cost_usd.is_(None)),
        func.count().filter(LLMCall.outcome == "succeeded", LLMCall.fallback_from.is_not(None)),
    ))).one()
    by_entry: dict[str, Any] = {}
    agg: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0, 0.0, 0])
    for provider, model, calls, errors, latency, cost, tokens in rows:
        by_entry[f"{provider}:{model}"] = _stats(calls, errors, float(latency), float(cost), int(tokens))
        a = agg[provider]
        a[0] += calls; a[1] += errors; a[2] += float(latency); a[3] += float(cost); a[4] += int(tokens)  # noqa: E702
    return {
        "totals": {"calls": totals[0], "failed": totals[1], "cost_usd": round(float(totals[2]), 6),
                   "input_tokens": int(totals[3]), "output_tokens": int(totals[4]), "unpriced_calls": totals[5]},
        "fallbacks": totals[6],
        "by_provider": {p: _stats(int(a[0]), int(a[1]), a[2], a[3], int(a[4])) for p, a in sorted(agg.items())},
        "by_entry": by_entry,
    }
