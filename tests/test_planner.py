"""Planner agent + planning service (repair loop, sensitive policy). FakeLLMClient only."""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.planner import Plan
from app.llm.fake_client import FakeLLMClient
from app.schemas.plan import PlanConstraints
from app.services.planning_service import PlanningError, plan


def t(key: str, agent: str = "researcher", deps: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"key": key, "name": key.replace("_", " ").title(), "description": f"Do {key} thoroughly.",
            "agent_type": agent, "depends_on": deps or [], "rationale": f"Needed for {key}.", **extra}


GOOD = {
    "plan_summary": "Research both, compare, write, review.",
    "tasks": [
        t("research_india"), t("research_china"),
        t("compare", "analyst", ["research_india", "research_china"]),
        t("write_report", "writer", ["compare"], input={"audience": "beginners"}),
        t("review_report", "reviewer", ["write_report", "research_india", "research_china"]),
    ],
}
CYCLIC = {"plan_summary": "bad", "tasks": [t("a", deps=["b"]), t("b", "writer", ["a"])]}


@pytest.fixture
def sensitive_agent() -> str:
    return "publisher"  # the real sensitive agent (Part 6)


async def test_prompt_lists_agents_and_excludes_planner() -> None:
    llm = FakeLLMClient({Plan: GOOD})
    await plan(llm, "Compare EV adoption in India and China", PlanConstraints(notes="keep it short"))
    call = llm.calls[0]
    system = call["system_prompt"]
    for agent in ("researcher", "analyst", "writer", "reviewer"):
        assert f"- {agent}:" in system
    assert "- planner" not in system
    assert "Output fields: summary, key_findings" in system
    assert "EXAMPLE OF A GOOD PLAN" in system and "PARALLEL" in system
    assert "Compare EV adoption in India and China" in call["user_prompt"]
    assert "keep it short" in call["user_prompt"]
    assert call["thinking_level"] == "high"  # planner default


async def test_valid_plan_accepted() -> None:
    result = await plan(FakeLLMClient({Plan: GOOD}), "goal")
    assert result.repair_attempts == 0
    assert [s.key for s in result.specs][:2] == ["research_india", "research_china"]
    assert result.execution_levels == [["research_china", "research_india"], ["compare"],
                                       ["write_report"], ["review_report"]]
    assert result.mermaid.startswith("graph TD")


async def test_invalid_then_valid_triggers_exactly_one_repair() -> None:
    llm = FakeLLMClient({Plan: [CYCLIC, GOOD]})
    result = await plan(llm, "goal")
    assert len(llm.calls) == 2 and result.repair_attempts == 1
    repair_prompt = llm.calls[1]["user_prompt"]
    assert "YOUR PREVIOUS PLAN WAS REJECTED" in repair_prompt
    assert "Dependency cycle: a -> b -> a" in repair_prompt
    assert '"key": "a"' in repair_prompt  # previous plan included


async def test_always_invalid_raises_planning_error_after_max_attempts() -> None:
    llm = FakeLLMClient({Plan: [CYCLIC] * 5})
    with pytest.raises(PlanningError) as info:
        await plan(llm, "goal")
    assert len(llm.calls) == 3  # 1 + PLANNER_MAX_REPAIR_ATTEMPTS (2)
    assert "Dependency cycle: a -> b -> a" in info.value.details
    assert info.value.status_code == 422


async def test_max_tasks_constraint_is_enforced() -> None:
    llm = FakeLLMClient({Plan: [GOOD, GOOD, GOOD]})
    with pytest.raises(PlanningError, match="maximum is 3"):
        await plan(llm, "goal", PlanConstraints(max_tasks=3))
    assert '"max_tasks": 3' in llm.calls[0]["user_prompt"]


async def test_sensitive_policy_forces_approval(sensitive_agent: str) -> None:
    proposed = {"plan_summary": "s", "tasks": [
        t("draft", "writer", requires_approval=True),       # LLM may add approval
        t("publish", sensitive_agent, ["draft"], requires_approval=False),  # LLM tried to skip it
    ]}
    llm = FakeLLMClient({Plan: proposed})
    result = await plan(llm, "goal")
    approvals = {s.key: s.requires_approval for s in result.specs}
    assert approvals == {"draft": True, "publish": True}
    assert result.forced_approvals == ["publish"]
    assert "[SENSITIVE: always requires approval]" in llm.calls[0]["system_prompt"]


async def test_sensitive_policy_on_manual_creation(sensitive_agent: str, client) -> None:  # noqa: ANN001
    resp = await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": [
        {"key": "publish", "name": "Publish", "description": "Publish it", "agent_type": sensitive_agent,
         "requires_approval": False},
    ]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["tasks"][0]["requires_approval"] is True
    agents = {a["agent_type"]: a for a in (await client.get("/agents")).json()}
    assert agents[sensitive_agent]["sensitive"] is True and agents["writer"]["sensitive"] is False


async def test_workers_use_low_thinking_level() -> None:
    from app.agents import AgentInput, create_agent

    llm = FakeLLMClient()
    await create_agent("writer", llm).run(AgentInput(workflow_goal="g", task_key="w", task_name="w"))
    assert llm.calls[0]["thinking_level"] == "low"
