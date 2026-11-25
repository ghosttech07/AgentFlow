from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.base import AgentInput, AgentResult, BaseAgent
from app.agents.prompts import today
from app.agents.registry import register_agent
from app.llm.base import LLMResponse, Source
from app.search import web


class ResearchOutput(BaseModel):
    summary: str
    key_findings: list[str] = Field(min_length=3, max_length=10)
    sources: list[Source]
    confidence: Literal["low", "medium", "high"]
    gaps: list[str] = Field(description="What could not be found or verified.")


CITE_RE = re.compile(r"\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")  # [2], [1, 4], [1;3]


def cited_numbers(texts: list[str]) -> set[int]:
    return {int(n) for t in texts for group in CITE_RE.findall(t) for n in re.split(r"[,;]", group)}


def research_queries(agent_input: AgentInput) -> list[str]:
    """Planner-written queries (task input "search_queries"), else the task itself and the goal."""
    queries = agent_input.task_input.get("search_queries")
    if isinstance(queries, list) and queries:
        return [str(q) for q in queries[:3]]
    first_sentence = re.split(r"(?<=[.!?])\s", agent_input.task_description or "", maxsplit=1)[0]
    return [first_sentence[:200] or agent_input.task_name, agent_input.workflow_goal[:200]]


def cited_sources(texts: list[str], ctx: web.WebContext) -> list[Source]:
    """Sources the model actually cited as [n]; all retrieved sources if it cited none."""
    cited = cited_numbers(texts)
    picked = [s for s in ctx.sources if s.n in cited] or ctx.sources
    return [Source(title=s.title, url=s.url) for s in picked]


@register_agent
class ResearcherAgent(BaseAgent):
    agent_type = "researcher"
    description = ("Researches a topic on the LIVE web (searches, reads the top pages) and returns cited "
                   "findings with sources. Give it search_queries in its input.")
    output_model = ResearchOutput
    use_search = True  # Gemini Google Search grounding, only when ENABLE_SEARCH_GROUNDING (billing)
    system_prompt = """You are a meticulous research analyst with access to freshly retrieved web pages.
Your job: extract accurate, current, verifiable facts for the task from the WEB SOURCES provided.
Rules:
- Base every finding on the WEB SOURCES (or on previous task results). Never invent facts, numbers, quotes, names or URLs.
- Cite every finding and important sentence with the source number in brackets, e.g. "EV sales rose 20% in 2026 [2]".
- Prefer the most recent data; always state the period or date of a figure ("Sept 2026", "FY2025-26").
- If sources disagree, give both values with their citations and say they disagree.
- Prefer specific facts (figures, dates, named entities) over generalities.
- key_findings: 3-10 short, self-contained, cited bullet points, most important first.
- sources: return an empty list (the system fills it in from what you cited).
- gaps: everything the sources did not cover or that you could not verify. Be honest.
- confidence: "high" only if key findings are backed by several reliable, recent sources; "low" if sources are thin, old or missing.
- summary: 2-5 neutral, cited sentences."""

    def __init__(self, llm):  # noqa: ANN001
        super().__init__(llm)
        self._web: web.WebContext | None = None

    def _uses_web_layer(self) -> bool:
        # Gemini grounding (billing) replaces the free web layer when it is enabled.
        return not self.settings.enable_search_grounding and web.provider_name(self.settings) != "none"

    async def run(self, agent_input: AgentInput) -> AgentResult:
        if self._uses_web_layer():
            self._web = await web.gather(research_queries(agent_input), self.settings)
        return await super().run(agent_input)

    def build_user_prompt(self, agent_input: AgentInput) -> str:
        prompt = super().build_user_prompt(agent_input)
        if self._web is not None:
            prompt += "\n" + self._web.as_prompt(today())
        return prompt

    def postprocess(self, data: ResearchOutput, response: LLMResponse) -> ResearchOutput:
        # Code decides the source list: grounding metadata, or the pages we actually retrieved.
        if response.sources:
            data.sources = response.sources
        elif self._web is not None:
            data.sources = cited_sources([data.summary, *data.key_findings], self._web)
            if not self._web.sources:
                data.gaps = [*data.gaps, "Live web search returned no usable pages for this task."]
                data.confidence = "low"
        return data
