"""Planning endpoints: auto_plan, /plan, /plans/preview, /graph. FakeLLMClient only."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.planner import Plan
from app.llm import get_llm_client
from app.llm.errors import LLMTransientError
from app.llm.fake_client import FakeLLMClient
from app.models import Task, Workflow, WorkflowEvent
from tests.test_planner import CYCLIC, GOOD, t


def manual(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Planner-style task dicts minus `rationale`, which the manual API does not accept."""
    return [{k: v for k, v in task.items() if k != "rationale"} for task in tasks]


def use_llm(app: FastAPI, llm: FakeLLMClient) -> FakeLLMClient:
    app.dependency_overrides[get_llm_client] = lambda: llm
    return llm


async def events(client: AsyncClient, wf_id: str) -> list[dict[str, Any]]:
    return (await client.get(f"/workflows/{wf_id}/events")).json()


def status_flow(evts: list[dict[str, Any]]) -> list[str]:
    flow = [e["payload"]["to"] for e in evts if e["event_type"] == "workflow.status_changed"]
    return ["PENDING", *flow]


async def test_auto_plan_creates_tasks_and_dependencies(app: FastAPI, client: AsyncClient) -> None:
    use_llm(app, FakeLLMClient({Plan: GOOD}))
    resp = await client.post("/workflows", json={"name": "EV", "goal": "Compare EVs", "auto_plan": True})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["context"]["plan_summary"] == GOOD["plan_summary"]
    deps = {task["key"]: task["depends_on"] for task in body["tasks"]}
    assert deps["compare"] == ["research_china", "research_india"]
    assert deps["review_report"] == ["research_china", "research_india", "write_report"]

    evts = await events(client, body["id"])
    assert status_flow(evts) == ["PENDING", "PLANNING", "PENDING"]
    types = [e["event_type"] for e in evts]
    assert types[:3] == ["workflow.created", "workflow.status_changed", "workflow.planning_started"]
    assert types.count("task.created") == 5
    plan_event = next(e for e in evts if e["event_type"] == "workflow.plan_created")
    assert plan_event["payload"]["task_count"] == 5
    assert plan_event["payload"]["repair_attempts"] == 0
    assert plan_event["payload"]["execution_levels"][0] == ["research_china", "research_india"]


async def test_planning_failure_marks_failed_and_replan_works(app: FastAPI, client: AsyncClient) -> None:
    use_llm(app, FakeLLMClient({Plan: [CYCLIC] * 3}))
    resp = await client.post("/workflows", json={"name": "x", "goal": "g", "auto_plan": True})
    assert resp.status_code == 201  # failure is reported in the body, not as a 500
    wf = resp.json()
    assert wf["status"] == "FAILED" and wf["tasks"] == []
    assert "Dependency cycle: a -> b -> a" in wf["error"]
    failed = next(e for e in await events(client, wf["id"]) if e["event_type"] == "workflow.planning_failed")
    assert failed["payload"]["error_type"] == "PlanningError"
    assert "Dependency cycle: a -> b -> a" in failed["payload"]["errors"]

    use_llm(app, FakeLLMClient({Plan: GOOD}))  # re-plan after failure
    again = await client.post(f"/workflows/{wf['id']}/plan")
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "PENDING" and again.json()["error"] is None
    assert len(again.json()["tasks"]) == 5
    assert status_flow(await events(client, wf["id"])) == [
        "PENDING", "PLANNING", "FAILED", "PLANNING", "PENDING"]


async def test_plan_endpoint_errors(app: FastAPI, client: AsyncClient) -> None:
    wf_id = (await client.post("/workflows", json={"name": "x", "goal": "g"})).json()["id"]

    use_llm(app, FakeLLMClient({Plan: [CYCLIC] * 3}))
    resp = await client.post(f"/workflows/{wf_id}/plan", json={"constraints": {"notes": "n"}})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "planning_failed"
    assert "Dependency cycle: a -> b -> a" in resp.json()["error"]["details"]

    use_llm(app, FakeLLMClient(error=LLMTransientError("Gemini request timed out")))
    resp = await client.post(f"/workflows/{wf_id}/plan")
    assert resp.status_code == 503  # Part 2 mapping; workflow marked FAILED, re-plannable
    assert (await client.get(f"/workflows/{wf_id}")).json()["status"] == "FAILED"


async def test_409_when_tasks_already_exist(app: FastAPI, client: AsyncClient) -> None:
    llm = use_llm(app, FakeLLMClient({Plan: GOOD}))
    wf = (await client.post("/workflows", json={"name": "x", "goal": "g", "tasks": manual([t("only")])})).json()
    resp = await client.post(f"/workflows/{wf['id']}/plan")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conflict"
    assert "already has 1 tasks" in resp.json()["error"]["message"]
    assert llm.calls == []
    # A planned workflow cannot be planned again either.
    planned = (await client.post("/workflows", json={"name": "y", "goal": "g", "auto_plan": True})).json()
    assert (await client.post(f"/workflows/{planned['id']}/plan")).status_code == 409


async def test_unknown_workflow_404(client: AsyncClient) -> None:
    assert (await client.post("/workflows/00000000-0000-0000-0000-000000000000/plan")).status_code == 404


async def test_preview_saves_nothing(app: FastAPI, client: AsyncClient, db_session: AsyncSession) -> None:
    use_llm(app, FakeLLMClient({Plan: [CYCLIC, GOOD]}))
    resp = await client.post("/plans/preview", json={"goal": "Compare EVs", "constraints": {"max_tasks": 8}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"]["plan_summary"] == GOOD["plan_summary"]
    assert body["repair_attempts"] == 1
    assert body["execution_levels"][-1] == ["review_report"]
    assert body["mermaid"].startswith("graph TD")
    for model in (Workflow, Task, WorkflowEvent):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_preview_planning_error_422(app: FastAPI, client: AsyncClient) -> None:
    use_llm(app, FakeLLMClient({Plan: [CYCLIC] * 3}))
    resp = await client.post("/plans/preview", json={"goal": "g"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "planning_failed"


async def test_graph_endpoint(app: FastAPI, client: AsyncClient) -> None:
    use_llm(app, FakeLLMClient({Plan: GOOD}))
    wf = (await client.post("/workflows", json={"name": "x", "goal": "g", "auto_plan": True})).json()
    graph = (await client.get(f"/workflows/{wf['id']}/graph")).json()

    assert [node["key"] for node in graph["nodes"]] == [task["key"] for task in GOOD["tasks"]]
    assert graph["nodes"][0] == {"key": "research_india", "name": "Research India",
                                 "agent_type": "researcher", "status": "PENDING", "requires_approval": False,
                                 # Part 10 fields (defaults for a plain task)
                                 "run_if": None, "join": "all_succeeded", "skip_reason": None, "loop_name": None,
                                 "iteration": 1, "base_key": "research_india"}
    assert {"from": "compare", "to": "write_report"} in graph["edges"]
    assert len(graph["edges"]) == 6
    assert graph["execution_levels"] == [["research_china", "research_india"], ["compare"],
                                         ["write_report"], ["review_report"]]
    assert "research_india --> compare" in graph["mermaid"]
    assert (await client.get("/workflows/00000000-0000-0000-0000-000000000000/graph")).status_code == 404


async def test_manual_creation_with_cycle_422(client: AsyncClient) -> None:
    resp = await client.post("/workflows", json={"name": "x", "goal": "g", "tasks": manual(CYCLIC["tasks"])})
    assert resp.status_code == 422
    assert "Dependency cycle: a -> b -> a" in resp.json()["error"]["details"]
