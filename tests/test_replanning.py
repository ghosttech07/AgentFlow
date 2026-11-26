"""Part 10 re-planning: triggers, validation (never undo finished work), approvals, limits, SUPERSEDED, recovery."""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import update

from app.agents.researcher import ResearchOutput
from app.core.plan_patch import PlanPatch
from app.llm.errors import LLMPermanentError
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models import Workflow
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key

OK = {"summary": "s", "key_findings": ["a", "b", "c"], "sources": [], "confidence": "high", "gaps": []}


def planned(key: str, agent: str = "researcher", deps: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "name": f"Task {key}", "description": f"Do {key}.", "agent_type": agent, "input": {},
            "depends_on": deps or [], "requires_approval": False, "rationale": "replacement"}


ALT = {"summary": "Use another source", "add_tasks": [planned("research_alt")],
       "rewire": [{"from_failed": "research_b", "to_replacement": "research_alt"}]}


def llm(*patches: dict, fail_key: str = "research_b") -> FakeLLMClient:
    return FakeLLMClient({PlanPatch: list(patches), ResearchOutput: OK},
                         scripts=[FakeScript(match=match(fail_key), error=LLMPermanentError("source is gone"))])


async def make(client: AsyncClient, replan_on_failure: bool = True, budget: dict | None = None) -> str:
    body: dict[str, Any] = {"name": "w", "goal": "Compare EVs", "replan_on_failure": replan_on_failure, "tasks": [
        task("research_a"), task("research_b", max_attempts=1), task("write", "writer", ["research_a", "research_b"])]}
    if budget:
        body["budget"] = budget
    wf = (await client.post("/workflows", json=body)).json()["id"]
    assert (await client.post(f"/workflows/{wf}/start")).status_code == 202
    return wf


async def versions(client: AsyncClient, wf: str) -> list[tuple]:
    return [(v["version"], v["trigger"], v["status"]) for v in (await client.get(f"/workflows/{wf}/plan-versions")).json()]


async def test_failure_triggers_a_replan_that_is_auto_applied(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await make(client)
    await make_engine(llm(ALT)).run_until_idle()
    t = await tasks_by_key(client, wf)
    assert t["research_b"]["status"] == "SUPERSEDED" and t["research_alt"]["status"] == "SUCCEEDED"
    assert set(t["write"]["depends_on"]) == {"research_a", "research_alt"}
    assert t["research_a"]["attempt_count"] == 1  # finished work was not redone
    assert (await detail(client, wf))["status"] == "COMPLETED"
    assert await versions(client, wf) == [(1, "initial", "applied"), (2, "task_failed", "applied")]
    types = [e["event_type"] for e in (await client.get(f"/workflows/{wf}/events")).json()]
    assert "workflow.replan_started" in types and "workflow.replan_applied" in types


async def test_without_opt_in_a_failure_still_fails(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await make(client, replan_on_failure=False)
    await make_engine(llm(ALT)).run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"


async def test_human_replan_works_on_a_manual_workflow(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await make(client, replan_on_failure=False)
    engine = make_engine(llm(ALT))
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"
    resp = await client.post(f"/workflows/{wf}/replan", json={"actor": "dana", "instructions": "use another source"})
    assert resp.status_code == 202 and resp.json()["status"] == "REPLANNING"
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "COMPLETED"
    assert await versions(client, wf) == [(1, "initial", "applied"), (2, "human", "applied")]


async def test_invalid_patches_are_repaired(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    bad_cancel = {"summary": "x", "add_tasks": [planned("research_alt")], "cancel_tasks": ["research_a"],
                  "rewire": [{"from_failed": "research_b", "to_replacement": "research_alt"}]}  # research_a SUCCEEDED
    wf = await make(client)
    engine = make_engine(llm(bad_cancel, ALT))
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "COMPLETED"
    prompts = [c["user_prompt"] for c in engine.llm.calls if "YOUR PREVIOUS PATCH WAS INVALID" in c["user_prompt"]]
    assert prompts and "never undone" in prompts[0]


async def test_rejected_patch_rules(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.agents.registry import task_agent_types
    from app.core.plan_patch import validate_patch
    from app.models import Task

    wf = await make(client)
    await make_engine(FakeLLMClient({PlanPatch: [{"summary": "nothing", "add_tasks": []}] * 9, ResearchOutput: OK},
                                    scripts=[FakeScript(match=match("research_b"), error=LLMPermanentError("x"))])
                      ).run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"  # every patch invalid -> automatic re-plan failed
    async with session_factory() as s:
        tasks = list(await s.scalars(select(Task).where(Task.workflow_id == uuid.UUID(wf))
                                     .options(selectinload(Task.dependencies))))
    kw = {"max_new_tasks": 5, "max_tasks": 20, "max_loop_iterations": 5}
    cycle = PlanPatch.model_validate({"summary": "c", "add_tasks": [planned("x", deps=["y"]), planned("y", deps=["x"])]})
    assert any("cycle" in e.message.lower() for e in validate_patch(cycle, tasks, task_agent_types(), **kw))
    running = PlanPatch.model_validate({"summary": "r", "add_tasks": [planned("z")],
                                        "rewire": [{"from_failed": "research_a", "to_replacement": "z"}]})
    assert any(e.code == "rewire_not_failed" for e in validate_patch(running, tasks, task_agent_types(), **kw))


async def test_sensitive_addition_needs_approval_and_reject_fails(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    patch = {**ALT, "add_tasks": [planned("research_alt"), planned("publish_now", "publisher", ["write"])]}
    wf = await make(client)
    engine = make_engine(llm(patch))
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "REPLANNING"
    a = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()[0]
    assert a["kind"] == "plan_patch" and a["task_id"] is None and "sensitive" in a["request_payload"]["why"][0]
    assert any("publish_now" in line for line in a["request_payload"]["diff"])
    resp = await client.post(f"/approvals/{a['id']}/reject", json={"actor": "dana", "reason": "no publishing"})
    assert resp.status_code == 200
    d = await detail(client, wf)
    assert d["status"] == "FAILED"
    ev = [e for e in (await client.get(f"/workflows/{wf}/events")).json() if e["event_type"] == "approval.rejected"][0]
    assert ev["payload"]["actor"] == "dana" and ev["payload"]["kind"] == "plan_patch"


async def test_approved_patch_is_applied(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    patch = {**ALT, "add_tasks": [planned("research_alt"), planned("extra_1"), planned("extra_2")]}  # 3 > auto 2
    wf = await make(client)
    engine = make_engine(llm(patch))
    await engine.run_until_idle()
    a = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()[0]
    assert (await client.post(f"/approvals/{a['id']}/approve", json={"actor": "dana"})).status_code == 200
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "COMPLETED"
    assert (await versions(client, wf))[-1] == (2, "task_failed", "applied")


async def test_cancel_while_a_plan_patch_waits(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    patch = {**ALT, "add_tasks": [planned("research_alt"), planned("publish_now", "publisher", ["write"])]}
    wf = await make(client)
    await make_engine(llm(patch)).run_until_idle()
    assert (await client.post(f"/workflows/{wf}/cancel", json={"actor": "dana"})).status_code == 200
    rows = (await client.get(f"/approvals?workflow_id={wf}")).json()
    assert [r["status"] for r in rows] == ["cancelled"]


async def test_plan_patch_approval_expires(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    from app.db.base import utcnow
    from app.models import Approval

    patch = {**ALT, "add_tasks": [planned("research_alt"), planned("publish_now", "publisher", ["write"])]}
    wf = await make(client)
    engine = make_engine(llm(patch))
    await engine.run_until_idle()
    async with session_factory() as s:
        await s.execute(update(Approval).where(Approval.workflow_id == uuid.UUID(wf)).values(expires_at=utcnow()))
        await s.execute(update(Workflow).where(Workflow.id == uuid.UUID(wf)).values(status="RUNNING"))
        await s.commit()
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"
    assert [r["status"] for r in (await client.get(f"/approvals?workflow_id={wf}")).json()] == ["expired"]


async def test_max_replans_is_enforced(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await make(client, budget={"max_replans": 0})
    await make_engine(llm(ALT)).run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"
    assert (await client.post(f"/workflows/{wf}/replan", json={"actor": "dana"})).status_code == 409


async def test_human_plan_patch_while_paused(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await started_workflow(client, [task("a"), task("b", "writer", ["a"])])  # a manual workflow
    assert (await client.post(f"/workflows/{wf}/plan-patch", json={"actor": "dana", "patch": {
        "summary": "add", "add_tasks": [planned("c", "researcher", ["a"])]}})).status_code == 409  # not PAUSED
    await client.post(f"/workflows/{wf}/pause", json={"actor": "dana"})
    resp = await client.post(f"/workflows/{wf}/plan-patch", json={"actor": "dana", "patch": {
        "summary": "add a check", "add_tasks": [planned("c", "researcher", ["a"])]}})
    assert resp.status_code == 200, resp.text
    assert "c" in {x["key"] for x in resp.json()["tasks"]} and resp.json()["status"] == "PAUSED"
    await client.post(f"/workflows/{wf}/resume", json={"actor": "dana"})
    await make_engine(FakeLLMClient()).run_until_idle()
    assert (await detail(client, wf))["status"] == "COMPLETED"
    assert (await versions(client, wf))[-1] == (2, "human_patch", "applied")


async def test_crash_during_replanning_recovers(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    wf = await make(client)
    await make_engine(FakeLLMClient({ResearchOutput: OK}, scripts=[FakeScript(
        match=match("research_b"), error=LLMPermanentError("x"))])).tick()  # nothing to see yet
    async with session_factory() as s:  # simulate: the planner job died while state was "planning"
        w = await s.get(Workflow, uuid.UUID(wf))
        w.status = "REPLANNING"
        w.context = {**w.context, "replan": {"state": "planning", "previous_status": "RUNNING", "trigger": "human",
                                             "reason": "test"}}
        await s.commit()
    engine = make_engine(llm(ALT), engine_single_node=True)
    await engine.recover()
    await engine.run_until_idle()
    d = await detail(client, wf)
    assert d["status"] in ("COMPLETED", "RUNNING", "FAILED") and len(await versions(client, wf)) >= 1
    assert d["status"] != "REPLANNING"


async def test_replan_if_trigger(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    weak = {**OK, "confidence": "low", "gaps": ["a", "b", "c", "d"]}
    body = {"name": "w", "goal": "g", "tasks": [
        task("research_x", replan_if="len(research_x.output.gaps) > 3"), task("write", "writer", ["research_x"])]}
    wf = (await client.post("/workflows", json=body)).json()["id"]
    await client.post(f"/workflows/{wf}/start")
    patch = {"summary": "dig deeper", "add_tasks": [planned("research_more", "researcher", ["research_x"])]}
    await make_engine(FakeLLMClient({ResearchOutput: [weak, OK, OK], PlanPatch: [patch]})).run_until_idle()
    t = await tasks_by_key(client, wf)
    assert "research_more" in t and (await versions(client, wf))[-1][1] == "replan_if"
