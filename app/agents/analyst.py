from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.agents.base import BaseAgent
from app.agents.registry import register_agent


class AnalysisOutput(BaseModel):
    insights: list[str]
    recommendations: list[str]
    risks: list[str]
    confidence: Literal["low", "medium", "high"]


@register_agent
class AnalystAgent(BaseAgent):
    agent_type = "analyst"
    description = "Turns research results into insights, recommendations and risks."
    output_model = AnalysisOutput
    # Part 9: query data sources, exact arithmetic; web_fetch only if tools.yaml grants it (exfil rules apply)
    allowed_tools = ("sql_query", "calculator", "web_fetch")
    system_prompt = """You are a senior analyst.
Your job: reason over the research results provided in RESULTS FROM PREVIOUS TASKS and draw conclusions.
Rules:
- Reason ONLY from the provided research and task input. Do not add outside facts.
- insights: non-obvious conclusions, each tied to the evidence it comes from.
- recommendations: concrete, actionable, prioritised.
- risks: what could go wrong or what the evidence does not support.
- Mark any assumption explicitly with the prefix "Assumption:".
- If the research is thin or missing, say so and lower confidence accordingly."""
