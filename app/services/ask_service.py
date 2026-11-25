"""Quick Ask: one question -> live web search -> one fast model call -> cited answer, in seconds.

No workflow, no DB rows: for quick lookups. Use a workflow for multi-step reports.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.prompts import today
from app.agents.researcher import cited_numbers
from app.config import Settings
from app.llm.base import LLMClient
from app.search import web

logger = logging.getLogger(__name__)


class AskOutput(BaseModel):
    answer: str = Field(description="Markdown answer; cite sources as [n] after the facts they support.")
    key_points: list[str] = Field(max_length=6, description="Up to 6 short cited takeaways.")
    confidence: Literal["low", "medium", "high"]
    follow_up_questions: list[str] = Field(max_length=3)


SYSTEM_PROMPT = """You answer questions using freshly retrieved WEB SOURCES. Be fast, direct and accurate.
Rules:
- Answer the question in the first sentence, then add the most useful detail. Usually under 200 words.
- Use the WEB SOURCES for anything current (prices, news, numbers, recent events). Cite every fact as [n].
- State dates or periods for figures. If sources disagree, say so and cite both.
- If the sources do not answer the question, say exactly that; you may add clearly-labelled general knowledge.
- Never invent numbers, names, quotes or URLs.
- confidence: "high" only with several recent, agreeing sources."""


async def ask(question: str, llm: LLMClient, settings: Settings) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = await web.gather([question], settings)
    prompt = f"## TODAY\n{today()}\n\n## QUESTION\n{question}\n\n{ctx.as_prompt(today())}"
    response = await llm.generate_structured(
        system_prompt=SYSTEM_PROMPT, user_prompt=prompt, output_model=AskOutput,
        thinking_level=settings.worker_thinking_level, model=settings.gemini_worker_model,
    )
    out: AskOutput = response.data  # type: ignore[assignment]
    cited = cited_numbers([out.answer, *out.key_points])
    total_ms = int((time.perf_counter() - started) * 1000)
    logger.info("ask.answered sources=%d cited=%d total_ms=%d model=%s", len(ctx.sources), len(cited),
                total_ms, response.model)
    return {
        "question": question,
        **out.model_dump(),
        "sources": [{"n": s.n, "title": s.title, "url": s.url, "domain": s.domain, "cited": s.n in cited}
                    for s in ctx.sources],
        "search_provider": ctx.provider,
        "model": response.model,
        "timings_ms": {"search": ctx.search_ms, "read_pages": ctx.fetch_ms, "model": response.latency_ms,
                       "total": total_ms},
        "warnings": ctx.errors + ([] if ctx.sources else ["No live web sources were retrieved."]),
    }
