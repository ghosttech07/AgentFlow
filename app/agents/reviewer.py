from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.agents.registry import register_agent
from app.llm.base import LLMResponse


class ReviewIssue(BaseModel):
    severity: Literal["low", "medium", "high"]
    description: str


class ReviewOutput(BaseModel):
    approved: bool
    score: int = Field(ge=1, le=10)
    issues: list[ReviewIssue]
    suggestions: list[str]
    summary: str


@register_agent
class ReviewerAgent(BaseAgent):
    agent_type = "reviewer"
    description = "Strictly reviews written content for accuracy, completeness and clarity."
    output_model = ReviewOutput
    system_prompt = """You are a strict, fair editor and fact-checker.
Your job: review the content produced by previous tasks.
Check, in this order:
1. Accuracy: every claim must be supported by the research in RESULTS FROM PREVIOUS TASKS. Unsupported or contradicted claims are high severity.
2. Completeness: does it fully address the OVERALL GOAL and the task input requirements (length, format, audience)?
3. Clarity: structure, readability, tone.
Rules:
- issues: each with severity "low", "medium" or "high" and a specific description (quote the problem text).
- approved must be false if there is ANY high-severity issue.
- score: 1-10, where 8+ means publishable as is.
- suggestions: concrete edits, not general advice.
- summary: 1-3 sentences with your verdict."""

    def postprocess(self, data: ReviewOutput, response: LLMResponse) -> ReviewOutput:
        if any(issue.severity == "high" for issue in data.issues):
            data.approved = False  # enforced in code, not left to the model
        return data
