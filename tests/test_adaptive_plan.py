"""Part 10 plan format: loops, run_if / join / replan_if, validation, repair, persistence, planner prompt."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.agents.planner import Plan, PlannerAgent
from app.core.adaptive_spec import LoopSpec
from app.core.dag import validate_adaptive
from app.llm.fake_client import FakeLLMClient
from app.services.planning_service import plan
from tests.engine_helpers import task


def t(key: str, agent: str = "researcher", deps: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"key": key, "name": key, "description": f"do {key}", "agent_type": agent, "input": {},
            "depends_on": deps or [], "requires_approval": False, "rationale": "r", **extra}


LOOP = {"name": "improve_draft", "body": ["write_guide", "review_guide"],
        "until": "review_guide.output.approved and review_guide.output.score >= 8", "max_iterations": 3,
        "on_exhausted": "continue_with_best", "feedback": {"from": "review_guide", "to": "write_guide"}}
TASKS = [t("research"), t("write_guide", "writer", ["research"]), t("review_guide", "reviewer", ["write_guide"])]


def errors(tasks: list[dict], loops: list[dict]) -> list[str]:
    p = Plan.model_validate({"plan_summary": "s", "tasks": tasks, "loops": loops})
    return [e.message for e in validate_adaptive(p.tasks, p.loops, max_loop_iterations=5)]


def test_valid_loop_and_branches() -> None:
    tasks = [*TASKS, t("deep", "researcher", ["research"], run_if="research.output.confidence == 'low'"),
             t("sum", "writer", ["deep", "review_guide"], join="any_succeeded")]
    assert errors(tasks, [LOOP]) == []


@pytest.mark.parametrize(("loop_patch", "fragment"), [
    ({"body": ["review_guide", "write_guide"]}, "contiguous chain"),
    ({"body": ["write_guide", "missing"]}, "unknown task 'missing'"),
    ({"until": "research.output.score > 1"}, "until may only use the loop's own tasks"),
    ({"until": "review_guide.output.score >"}, "invalid condition"),
    ({"max_iterations": 9}, "max_iterations"),
    ({"feedback": {"from": "research", "to": "write_guide"}}, "feedback"),
])
def test_invalid_loops(loop_patch: dict, fragment: str) -> None:
    assert any(fragment in e for e in errors(TASKS, [{**LOOP, **loop_patch}])), errors(TASKS, [{**LOOP, **loop_patch}])


def test_run_if_may_only_use_dependencies() -> None:
    errs = errors([*TASKS, t("deep", "researcher", [], run_if="research.output.confidence == 'low'")], [])
    assert any("run_if may only use its dependencies" in e for e in errs)


def test_long_loop_key_has_a_clear_error() -> None:
    long_key = "write_a_very_detailed_beginner_guide_for_readers"  # 48 chars
    tasks = [t("research"), t(long_key, "writer", ["research"]), t("review_guide", "reviewer", [long_key])]
    loop = {**LOOP, "body": [long_key, "review_guide"], "feedback": {"from": "review_guide", "to": long_key}}
    assert any("loop body task key must be <= 46 characters so __vN fits" in e for e in errors(tasks, [loop]))


async def test_planner_repair_shortens_a_long_loop_key() -> None:
    long_key = "write_a_very_detailed_beginner_guide_for_readers"
    bad = {"plan_summary": "s", "tasks": [t("research"), t(long_key, "writer", ["research"]),
                                          t("review_guide", "reviewer", [long_key])],
           "loops": [{**LOOP, "body": [long_key, "review_guide"], "feedback": {"from": "review_guide", "to": long_key}}]}
    good = {"plan_summary": "s", "tasks": TASKS, "loops": [LOOP]}
    llm = FakeLLMClient({Plan: [bad, good]})
    result = await plan(llm, "Write a guide")
    assert result.repair_attempts == 1 and result.plan.loops[0].body == ["write_guide", "review_guide"]
    assert "loop body task key must be <= 46 characters" in llm.calls[1]["user_prompt"]


async def test_manual_workflow_with_a_loop_is_stored(client: AsyncClient) -> None:
    body = {"name": "w", "goal": "g", "loops": [LOOP], "tasks": [
        task("research"), task("write_guide", "writer", ["research"]), task("review_guide", "reviewer", ["write_guide"]),
        task("deep", "researcher", ["research"], run_if="research.output.confidence == 'low'", join="all_done")]}
    resp = await client.post("/workflows", json=body)
    assert resp.status_code == 201, resp.text
    wf = resp.json()
    by_key = {x["key"]: x for x in wf["tasks"]}
    assert by_key["write_guide"]["loop_name"] == "improve_draft" and by_key["write_guide"]["iteration"] == 1
    assert by_key["deep"]["run_if"] == "research.output.confidence == 'low'" and by_key["deep"]["join"] == "all_done"
    versions = (await client.get(f"/workflows/{wf['id']}/plan-versions")).json()
    assert [(v["version"], v["trigger"], v["status"]) for v in versions] == [(1, "initial", "applied")]


async def test_manual_workflow_with_a_bad_condition_is_422(client: AsyncClient) -> None:
    resp = await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": [
        task("a"), task("b", "writer", ["a"], run_if="a.output.__class__")]})
    assert resp.status_code == 422


def test_planner_prompt_explains_loops_and_branches() -> None:
    prompt = PlannerAgent(FakeLLMClient()).build_system_prompt()
    assert "loops" in prompt and "run_if" in prompt and "__v" not in prompt.split("RULES")[0]
    assert "max_iterations" in prompt


def test_loop_spec_aliases_feedback_from() -> None:
    spec = LoopSpec.model_validate(LOOP)
    assert spec.feedback.from_ == "review_guide" and spec.model_dump(by_alias=True)["feedback"]["from"] == "review_guide"
