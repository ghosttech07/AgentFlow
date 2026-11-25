from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.agents.registry import register_agent
from app.llm.base import LLMResponse

WORD_RE = re.compile(r"\w+(?:['’-]\w+)*")


class WritingOutput(BaseModel):
    title: str
    content: str = Field(description="The full text in Markdown.")
    word_count: int = Field(ge=0, description="Recomputed by code after generation.")


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


@register_agent
class WriterAgent(BaseAgent):
    agent_type = "writer"
    description = "Writes Markdown content (reports, articles, summaries) from previous task results."
    output_model = WritingOutput
    system_prompt = """You are a professional writer.
Your job: turn the results from previous tasks into clear, well-structured Markdown.
Rules:
- Use ONLY facts from RESULTS FROM PREVIOUS TASKS and TASK INPUT. Never add facts, figures or sources of your own.
- Write for the audience stated in the task or task input; default to an informed general reader.
- Respect any length, tone, format or section requirements in the task input exactly.
- Use headings, short paragraphs and lists where they help readability.
- If the provided material is insufficient for part of the request, say so in the text rather than filling the gap.
- content is Markdown; title is plain text."""

    def postprocess(self, data: WritingOutput, response: LLMResponse) -> WritingOutput:
        data.word_count = count_words(data.content)  # models miscount; code is authoritative
        return data
