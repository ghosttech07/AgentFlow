"""LLM_PROVIDER=fake: realistic canned output for every agent, for demos without model quota.

The planner always returns a valid parallel plan for any goal:
  research_background, research_current_state  (parallel)
    -> analyze_findings -> write_report -> review_report [-> publish_report if the goal says "publish"]
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.analyst import AnalysisOutput
from app.agents.planner import Plan
from app.agents.researcher import ResearchOutput
from app.agents.reviewer import ReviewOutput
from app.agents.writer import WritingOutput, count_words
from app.llm.fake_client import FakeLLMClient
from app.services.ask_service import AskOutput


def _goal(prompt: str) -> str:
    match = re.search(r"## (?:OVERALL )?GOAL\n(.+?)\n", prompt)
    return match.group(1).strip() if match else "the requested topic"


def _plan(prompt: str) -> dict[str, Any]:
    goal = _goal(prompt)
    plan = {
        "plan_summary": f"Research background and current state in parallel, analyze them, "
                        f"then write and review a report for: {goal}",
        "tasks": [
            {"key": "research_background", "name": "Research background", "agent_type": "researcher",
             "description": f"Collect the key background facts and history relevant to: {goal}",
             "input": {"max_findings": 5, "search_queries": [goal[:80], f"{goal[:60]} history"]},
             "depends_on": [], "requires_approval": False, "rationale": "Context the report needs."},
            {"key": "research_current_state", "name": "Research current state", "agent_type": "researcher",
             "description": f"Collect recent data (last 2-3 years) and trends for: {goal}",
             "input": {"max_findings": 5}, "depends_on": [], "requires_approval": False,
             "rationale": "Up-to-date facts, independent of the background research."},
            {"key": "analyze_findings", "name": "Analyze findings", "agent_type": "analyst",
             "description": "Compare the background and current research; draw insights and risks.",
             "input": {}, "depends_on": ["research_background", "research_current_state"],
             "requires_approval": False, "rationale": "Turns facts into insights."},
            {"key": "write_report", "name": "Write report", "agent_type": "writer",
             "description": f"Write a clear, beginner-friendly report on: {goal}",
             "input": {"audience": "beginners", "length": "300 words"},
             "depends_on": ["analyze_findings", "research_background", "research_current_state"],
             "requires_approval": False, "rationale": "The final deliverable."},
            {"key": "review_report", "name": "Review report", "agent_type": "reviewer",
             "description": "Check the report against the research for accuracy and clarity.",
             "input": {}, "depends_on": ["write_report", "research_background", "research_current_state"],
             "requires_approval": False, "rationale": "Quality gate."},
        ],
    }
    if "publish" in goal.lower():  # sensitive step: the planning policy forces requires_approval
        plan["tasks"].append(
            {"key": "publish_report", "name": "Publish report", "agent_type": "publisher",
             "description": "Publish the reviewed report.", "input": {},
             "depends_on": ["write_report", "review_report"], "requires_approval": True,
             "rationale": "The goal asks to publish the result."})
    return plan


def _research(prompt: str) -> dict[str, Any]:
    goal = _goal(prompt)
    return {
        "summary": f"[demo] Key facts gathered for: {goal}.",
        "key_findings": [f"[demo] Finding {i} about {goal}" for i in range(1, 5)],
        "sources": [], "confidence": "medium",
        "gaps": ["[demo] Canned output from LLM_PROVIDER=fake; no real research was done."],
    }


def _analysis(prompt: str) -> dict[str, Any]:
    return {
        "insights": ["[demo] The two research streams agree on the main trend.",
                     "[demo] Recent data shows faster change than the historical average."],
        "recommendations": ["[demo] Focus the report on the three clearest trends."],
        "risks": ["Assumption: [demo] canned data stands in for real research."],
        "confidence": "medium",
    }


def _writing(prompt: str) -> dict[str, Any]:
    goal = _goal(prompt)
    content = (f"# {goal}\n\n## Overview\n\n[demo] This report was produced by AgentFlow in fake "
               "mode. Two research tasks ran in parallel, an analyst combined them, and this "
               "writer turned the results into a short guide.\n\n## Key points\n\n"
               "- [demo] Background facts summarised.\n- [demo] Current trends summarised.\n"
               "- [demo] Insights and risks from the analysis.\n")
    return {"title": f"[demo] {goal}"[:200], "content": content, "word_count": count_words(content)}


def _review(prompt: str) -> dict[str, Any]:
    return {"approved": True, "score": 8, "issues": [],
            "suggestions": ["[demo] Replace canned content with real research (LLM_PROVIDER=gemini)."],
            "summary": "[demo] Structure and clarity are good; content is placeholder."}


def _ask(prompt: str) -> dict[str, Any]:
    match = re.search(r"## QUESTION\n(.+?)\n", prompt)
    question = match.group(1).strip() if match else "your question"
    has_sources = "[1]" in prompt
    cite = " [1]" if has_sources else ""
    return {"answer": f"[demo] Canned answer to: {question}.{cite} With LLM_PROVIDER=gemini the model "
                      "reads the live web sources listed below and answers from them.",
            "key_points": [f"[demo] The sources below were really retrieved from the web{cite}."],
            "confidence": "low", "follow_up_questions": ["[demo] Try a real question with LLM_PROVIDER=gemini."]}


def build_demo_client(delay: float) -> FakeLLMClient:
    return FakeLLMClient(
        {
            Plan: _plan,  # type: ignore[dict-item]
            ResearchOutput: _research,  # type: ignore[dict-item]
            AnalysisOutput: _analysis,  # type: ignore[dict-item]
            WritingOutput: _writing,  # type: ignore[dict-item]
            ReviewOutput: _review,  # type: ignore[dict-item]
            AskOutput: _ask,  # type: ignore[dict-item]
        },
        delay=delay,
    )
