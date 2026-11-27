"""Part 9 agents: tool allowlists, the planner's tool-aware catalog, the communicator, the fake demo plan."""

from __future__ import annotations

import pytest

from app.agents.analyst import AnalystAgent
from app.agents.communicator import CommunicatorAgent, deliveries_from
from app.agents.planner import PlannerAgent, plannable_agent_types
from app.agents.researcher import ResearcherAgent
from app.agents.writer import WriterAgent
from app.llm.demo_fake import _plan, tool_step
from app.llm.fake_client import FakeLLMClient
from app.tools import config as tools_config
from tests.tool_helpers import use_tools


def test_agents_declare_their_tools() -> None:
    assert ResearcherAgent.allowed_tools == ("web_search", "web_fetch")
    assert AnalystAgent.allowed_tools == ("sql_query", "calculator", "web_fetch")  # web_fetch only if tools.yaml grants it
    assert WriterAgent.allowed_tools == ("file_write",)
    assert CommunicatorAgent.allowed_tools == ("send_email", "webhook_post") and CommunicatorAgent.requires_tools


def test_planner_catalog_lists_tools_and_the_communicator_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    use_tools(monkeypatch)
    prompt = PlannerAgent(FakeLLMClient()).build_system_prompt()
    assert "- researcher: " in prompt and "Tools: web_search, web_fetch." in prompt
    assert "- communicator: " in prompt and "Tools: send_email, webhook_post." in prompt
    assert "Use the communicator ONLY when the goal explicitly asks to send" in prompt


def test_communicator_is_not_offered_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_config, "get_tools_config", lambda: None)
    assert "communicator" not in plannable_agent_types()
    assert "- communicator:" not in PlannerAgent(FakeLLMClient()).build_system_prompt()
    assert "Tools:" not in PlannerAgent(FakeLLMClient()).build_system_prompt()  # v1 prompt shape without tools.yaml


def test_deliveries_come_from_the_journal_not_the_model() -> None:
    calls = [
        {"tool": "send_email", "status": "succeeded", "arguments": {"to": ["team@example.com"], "subject": "EV"}, "call_id": "1"},
        {"tool": "send_email", "status": "denied", "error": "not now", "arguments": {"to": ["boss@example.com"], "subject": "x"}},
        {"tool": "send_email", "status": "rejected_invalid", "arguments": {"to": "bad"}},
        {"tool": "web_search", "status": "succeeded", "arguments": {"query": "q"}},
    ]
    out = deliveries_from(calls)
    assert [(d.to, d.status) for d in out] == [("team@example.com", "sent"),
                                               ("boss@example.com", "not sent: denied by a human (not now)")]


def test_fake_demo_plan_for_an_email_goal() -> None:
    plan = _plan("## GOAL\nResearch the latest EV sales trends in India, compare them with our sample sales "
                 "database, write a short summary, and email it to team@example.com\n")
    by_key = {t["key"]: t for t in plan["tasks"]}
    assert [t["agent_type"] for t in plan["tasks"]] == ["researcher", "analyst", "writer", "communicator"]
    assert by_key["research_trends"]["depends_on"] == by_key["analyse_sales"]["depends_on"] == []  # parallel
    assert by_key["email_summary"]["input"]["recipients"] == ["team@example.com"]


def test_fake_tool_policy_walks_each_agent_through_its_tools() -> None:
    from app.agents.researcher import ResearchOutput

    goal = "## GOAL\nEV trends\n"
    assert tool_step(ResearchOutput, goal)["tool"] == "web_search"
    assert tool_step(ResearchOutput, goal + "Step 1: web_search({})\n")["tool"] == "web_fetch"
    last = tool_step(ResearchOutput, goal + "Step 1: web_search({})\nStep 2: web_fetch({})\n")
    assert last["action"] == "final" and any("made-up" in s["url"] for s in last["output"]["sources"])
