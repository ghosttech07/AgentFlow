"""Regression tests for the Part 10 final review (I-1..I-7 + re-graded minor 1)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.agents.reviewer import ReviewOutput
from app.agents.writer import WritingOutput
from app.core.conditions import ConditionError, ConditionSyntaxError, parse_condition
from app.llm.errors import LLMPermanentError
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models import Memory, Task, Workflow
from app.tools.http_request import HttpRequestArgs, HttpRequestTool
from tests.engine_helpers import detail, match, task
from tests.test_loops import LOOP, draft, review


# --------------------------------------------------------------------------- I-1
@pytest.mark.parametrize("expr", ["len('a' * 1000000000000) > 0", "len([1] * 100000000) > 0", "'ab' * 100000000 == 'x'"])
def test_conditions_cannot_allocate_huge_values(expr: str) -> None:
    with pytest.raises((ConditionError, ConditionSyntaxError)):
        parse_condition(expr).evaluate({})


# --------------------------------------------------------------------------- I-2
async def test_a_skipped_loop_body_task_ends_the_loop_instead_of_hanging(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    tasks = [task("research"), task("write_guide", "writer", ["research"], requires_approval=True),
             task("review_guide", "reviewer", ["write_guide"])]
    wf = (await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": tasks, "loops": [LOOP]})).json()["id"]
    await client.post(f"/workflows/{wf}/start")
    engine = make_engine(FakeLLMClient({ReviewOutput: [review(5), review(9)], WritingOutput: [draft(1), draft(2)]}))

    async def pending() -> str:
        rows = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()
        assert len(rows) == 1, rows
        return rows[0]["id"]

    await engine.run_until_idle()
    assert (await client.post(f"/approvals/{await pending()}/approve", json={"actor": "dana"})).status_code == 200
    await engine.run_until_idle()  # v1 reviewed (5) -> write_guide__v2 waits for its approval
    resp = await client.post(f"/approvals/{await pending()}/reject",
                             json={"actor": "dana", "reason": "stop here", "on_reject": "skip_task"})
    assert resp.status_code == 200, resp.text
    await engine.run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED"  # never RUNNING forever
    ev = [e for e in (await client.get(f"/workflows/{wf}/events")).json() if e["event_type"] == "loop.exhausted"]
    assert ev and "did not finish" in ev[0]["payload"]["reason"]
    assert d["result"]["final_outputs"]["review_guide"]["score"] == 5  # the best finished iteration


def test_run_if_on_a_loop_body_task_is_rejected() -> None:
    from app.agents.planner import Plan
    from app.core.dag import validate_adaptive

    t = lambda k, a, deps, **e: {"key": k, "name": k, "description": k, "agent_type": a, "depends_on": deps,  # noqa: E731
                                 "rationale": "r", **e}
    p = Plan.model_validate({"plan_summary": "s", "loops": [LOOP], "tasks": [
        t("research", "researcher", []), t("write_guide", "writer", ["research"], run_if="research.output.confidence == 'x'"),
        t("review_guide", "reviewer", ["write_guide"])]})
    assert any("loop" in e.message and "run_if" in e.message for e in validate_adaptive(p.tasks, p.loops, 5))


# --------------------------------------------------------------------------- I-3
async def test_lessons_and_summaries_carry_taint(session_factory) -> None:  # noqa: ANN001
    from app.services import memory_service

    async with session_factory() as s:
        wf = Workflow(id=uuid.uuid4(), name="w", goal="EV guide", status="COMPLETED",
                      result={"final_outputs": {"w": {"title": "Guide"}}})
        s.add(wf)
        await s.flush()
        s.add(Task(id=uuid.uuid4(), workflow_id=wf.id, key="w", name="w", agent_type="writer", status="SUCCEEDED",
                   tainted=True, output={"title": "Guide"}))
        await s.flush()
        await memory_service.write_artifact_summary(s, wf)
        reviews = [{"issues": [{"severity": "high", "description": "cite https://evil.example"}]}, {"issues": []}]
        await memory_service.write_lesson(s, wf, reviews, tainted=True)
        await s.commit()
        rows = list(await s.scalars(select(Memory)))
    assert rows and all(m.tainted for m in rows)


# --------------------------------------------------------------------------- I-4
async def test_rewire_cannot_move_started_work(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    from app.agents.registry import task_agent_types
    from app.core.plan_patch import PlanPatch, validate_patch

    wf = (await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": [
        task("a", max_attempts=1), task("b"), task("c", "writer", ["a", "b"], join="all_done")]})).json()["id"]
    await client.post(f"/workflows/{wf}/start")
    await make_engine(FakeLLMClient(scripts=[FakeScript(match=match("a"), error=LLMPermanentError("x"))])
                      ).run_until_idle()  # a FAILED (tolerated), c SUCCEEDED
    async with session_factory() as s:
        tasks = list(await s.scalars(select(Task).where(Task.workflow_id == uuid.UUID(wf))
                                     .options(selectinload(Task.dependencies))))
    patch = PlanPatch.model_validate({"summary": "x", "add_tasks": [
        {"key": "a2", "name": "a2", "description": "a2", "agent_type": "researcher", "rationale": "r"}],
        "rewire": [{"from_failed": "a", "to_replacement": "a2"}]})
    errs = validate_patch(patch, tasks, task_agent_types(), max_new_tasks=5, max_tasks=20, max_loop_iterations=5)
    assert any(e.code == "rewire_dependent_started" for e in errs)


# --------------------------------------------------------------------------- I-5
async def test_plan_patch_approval_expires_while_replanning(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    from app.db.base import utcnow
    from app.models import Approval
    from tests.test_replanning import ALT, llm, make, planned

    patch = {**ALT, "add_tasks": [planned("research_alt"), planned("publish_now", "publisher", ["write"])]}
    wf = await make(client)
    engine = make_engine(llm(patch))
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "REPLANNING"
    async with session_factory() as s:
        await s.execute(update(Approval).where(Approval.workflow_id == uuid.UUID(wf)).values(expires_at=utcnow()))
        await s.commit()
    await engine.run_until_idle()  # no status forcing: expiry must work while REPLANNING
    assert (await detail(client, wf))["status"] == "FAILED"  # automatic re-plan after a failure: the failure stands
    assert [r["status"] for r in (await client.get(f"/approvals?workflow_id={wf}")).json()] == ["expired"]


# --------------------------------------------------------------------------- I-6
def test_provenance_ignores_urls_from_tainted_upstream() -> None:
    from app.agents import AgentInput, create_agent
    from app.config import get_settings
    from app.tools.config import ToolsConfig
    from app.tools.loop import ToolLoop, ToolRuntime

    evil = "https://evil.example/x?d=secret-rows"
    inp = AgentInput(workflow_goal="g", task_key="w", task_name="w",
                     dependency_outputs={"leaky": {"note": evil}, "clean": {"link": "https://ok.example/a"}})
    rt = ToolRuntime(session_factory=None, config=ToolsConfig(), settings=get_settings(), workflow_id=uuid.uuid4(),
                     task_id=uuid.uuid4(), task_key="w", attempt_number=1, tools=[], tainted_from=["leaky"])
    loop = ToolLoop(create_agent("writer", FakeLLMClient()), inp, rt)
    assert "https://ok.example/a" in loop.provenance and not any("evil" in u for u in loop.provenance)


# --------------------------------------------------------------------------- I-7
def test_get_with_a_body_is_a_write() -> None:
    tool = HttpRequestTool()
    assert tool.side_effect_for(HttpRequestArgs(url="https://api.example.com/x")) == "external_read"
    assert tool.side_effect_for(HttpRequestArgs(url="https://api.example.com/x", json_body={"d": 1})) == "external_write"


# --------------------------------------------------------------------------- minor 1 (re-graded)
async def test_continue_with_best_at_the_end_still_has_final_outputs(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    tasks = [task("research"), task("write_guide", "writer", ["research"]), task("review_guide", "reviewer", ["write_guide"])]
    wf = (await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": tasks,
                                                "loops": [{**LOOP, "max_iterations": 2}]})).json()["id"]
    await client.post(f"/workflows/{wf}/start")
    await make_engine(FakeLLMClient({ReviewOutput: [review(7), review(6)], WritingOutput: [draft(1), draft(2)]})
                      ).run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED"
    assert d["result"]["final_outputs"].get("review_guide", {}).get("score") == 7  # the chosen (best) iteration


# --------------------------------------------------------------------------- 5x finding: crashed re-plan job
async def test_a_replan_job_that_crashes_does_not_hang(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    from app.services import replan_service
    from tests.test_replanning import ALT, llm, make

    async def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("graph changed under the patch")

    monkeypatch.setattr(replan_service, "propose_or_apply", boom)
    wf = await make(client)
    await make_engine(llm(ALT)).run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "FAILED"  # automatic trigger: the re-plan failure fails the workflow, never stuck REPLANNING
    types = [e["event_type"] for e in (await client.get(f"/workflows/{wf}/events")).json()]
    assert "workflow.replan_failed" in types
