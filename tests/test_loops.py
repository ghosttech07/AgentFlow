"""Part 10 feedback loops: dynamic expansion (<key>__v2), bounded, budgeted, idempotent, auditable."""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents.reviewer import ReviewOutput
from app.agents.writer import WritingOutput
from app.llm.fake_client import FakeLLMClient
from app.models import Task, Workflow
from app.services import graph_service
from tests.engine_helpers import detail, task, tasks_by_key


def review(score: int, approved: bool | None = None, issues: list[dict] | None = None) -> dict[str, Any]:
    return {"approved": score >= 8 if approved is None else approved, "score": score,
            "issues": issues if issues is not None else ([] if score >= 8 else
                                                          [{"severity": "high", "description": "Missing China data"}]),
            "suggestions": ["Add a comparison table"], "summary": f"score {score}"}


def draft(n: int, changes: list[str] | None = None) -> dict[str, Any]:
    return {"title": f"Draft {n}", "content": f"draft {n} text", "word_count": 3, "changes_made": changes or []}


LOOP = {"name": "improve_draft", "body": ["write_guide", "review_guide"],
        "until": "review_guide.output.approved and review_guide.output.score >= 8", "max_iterations": 3,
        "on_exhausted": "continue_with_best", "feedback": {"from": "review_guide", "to": "write_guide"}}


async def start(client: AsyncClient, loop: dict | None = None, extra_tasks: list[dict] | None = None,
                budget: dict | None = None) -> str:
    tasks = [task("research"), task("write_guide", "writer", ["research"]),
             task("review_guide", "reviewer", ["write_guide"]), *(extra_tasks or [])]
    body: dict[str, Any] = {"name": "w", "goal": "Write a guide", "tasks": tasks, "loops": [loop or LOOP]}
    if budget:
        body["budget"] = budget
    resp = await client.post("/workflows", json=body)
    assert resp.status_code == 201, resp.text
    wf = resp.json()["id"]
    assert (await client.post(f"/workflows/{wf}/start")).status_code == 202
    return wf


def llm(reviews: list[dict], drafts: list[dict] | None = None) -> FakeLLMClient:
    return FakeLLMClient({ReviewOutput: reviews, WritingOutput: drafts or [draft(i + 1) for i in range(len(reviews))]})


async def loop_events(client: AsyncClient, wf: str) -> list[dict]:
    return [e for e in (await client.get(f"/workflows/{wf}/events")).json() if e["event_type"].startswith("loop.")]


async def test_until_true_at_v1_means_no_expansion(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client)
    await make_engine(llm([review(9)])).run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED" and len(d["tasks"]) == 3
    ev = await loop_events(client, wf)
    assert [e["event_type"] for e in ev] == ["loop.condition_evaluated", "loop.completed"]
    assert ev[0]["payload"]["result"] is True and ev[0]["payload"]["values"]["review_guide.output.score"] == 9


async def test_false_inserts_v2_with_feedback_and_rewires_downstream(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, extra_tasks=[task("final_note", "writer", ["review_guide"])])
    drafts = [draft(1), draft(2, ["Added China data"]), draft(3)]
    await make_engine(llm([review(5), review(9)], drafts)).run_until_idle()
    t = await tasks_by_key(client, wf)
    assert set(t) == {"research", "write_guide", "review_guide", "write_guide__v2", "review_guide__v2", "final_note"}
    v2 = t["write_guide__v2"]
    assert v2["iteration"] == 2 and v2["base_key"] == "write_guide" and v2["loop_name"] == "improve_draft"
    assert set(v2["depends_on"]) == {"research", "write_guide", "review_guide"}
    fb = v2["input"]["revision_feedback"]
    assert fb["issues"][0]["description"] == "Missing China data" and fb["previous_draft"]["title"] == "Draft 1"
    assert t["write_guide__v2"]["output"]["changes_made"] == ["Added China data"]
    assert t["final_note"]["depends_on"] == ["review_guide__v2"]  # rewired to the last iteration
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED"
    assert [e["event_type"] for e in await loop_events(client, wf)] == [
        "loop.condition_evaluated", "loop.iteration_started", "loop.condition_evaluated", "loop.completed"]


async def test_downstream_waits_while_the_loop_is_open(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, extra_tasks=[task("final_note", "writer", ["review_guide"])])
    engine = make_engine(llm([review(5), review(9)]))
    await engine.run_until_idle()
    calls = [c["user_prompt"] for c in engine.llm.calls]
    note = next(i for i, p in enumerate(calls) if "Task final_note" in p)
    v2 = next(i for i, p in enumerate(calls) if "Task review_guide" in p and "revision_feedback" not in p and i > 3)
    assert note > v2  # final_note only ran after the loop finished
    assert (await detail(client, wf))["status"] == "COMPLETED"


async def test_exhausted_fail(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, {**LOOP, "max_iterations": 2, "on_exhausted": "fail"})
    await make_engine(llm([review(5), review(6)])).run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "FAILED" and "improve_draft" in d["error"]


async def test_exhausted_continue_with_best_picks_the_best_iteration(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, {**LOOP, "max_iterations": 2}, extra_tasks=[task("final_note", "writer", ["review_guide"])])
    await make_engine(llm([review(7), review(6)])).run_until_idle()
    t = await tasks_by_key(client, wf)
    assert t["final_note"]["depends_on"] == ["review_guide"]  # v1 scored best
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED" and "review_guide__v2" not in d["result"]["final_outputs"]
    ex = [e for e in await loop_events(client, wf) if e["event_type"] == "loop.exhausted"][0]["payload"]
    assert ex["chosen_iteration"] == 1 and ex["mode"] == "continue_with_best"


async def test_exhausted_ask_human_grants_more_iterations(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, {**LOOP, "max_iterations": 1, "on_exhausted": "ask_human"})
    engine = make_engine(llm([review(5), review(9)]))
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "AWAITING_APPROVAL"
    a = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()[0]
    assert a["kind"] == "loop_exhausted" and a["task_id"] is None
    resp = await client.post(f"/approvals/{a['id']}/approve", json={"actor": "dana", "extra_iterations": 1})
    assert resp.status_code == 200, resp.text
    await engine.run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED" and "review_guide__v2" in {x["key"] for x in d["tasks"]}


async def test_exhausted_ask_human_reject_fails(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, {**LOOP, "max_iterations": 1, "on_exhausted": "ask_human"})
    engine = make_engine(llm([review(5)]))
    await engine.run_until_idle()
    a = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()[0]
    assert (await client.post(f"/approvals/{a['id']}/reject", json={"actor": "dana", "reason": "good enough? no"})
            ).status_code == 200
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"


async def test_expansion_is_idempotent(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    wf = await start(client, {**LOOP, "max_iterations": 1, "on_exhausted": "ask_human"})
    await make_engine(llm([review(5)])).run_until_idle()
    async with session_factory() as s:
        w = await s.get(Workflow, uuid.UUID(wf))
        tasks = list(await s.scalars(select(Task).where(Task.workflow_id == w.id).options(selectinload(Task.dependencies))))
        first = await graph_service.expand_loop(s, w, tasks, "improve_draft", 2)
        tasks = list(await s.scalars(select(Task).where(Task.workflow_id == w.id).options(selectinload(Task.dependencies))))
        again = await graph_service.expand_loop(s, w, tasks, "improve_draft", 2)  # e.g. after a crash / second engine
        await s.commit()
        assert {t.key for t in first} == {t.key for t in again} == {"write_guide__v2", "review_guide__v2"}
        count = len(list(await s.scalars(select(Task).where(Task.workflow_id == w.id))))
    assert count == 5


async def test_budget_stops_iterations(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await start(client, budget={"max_iterations_total": 1})
    await make_engine(llm([review(5), review(5), review(5)])).run_until_idle()
    keys = {x["key"] for x in (await detail(client, wf))["tasks"]}
    assert "review_guide__v2" in keys and "review_guide__v3" not in keys
    ex = [e for e in await loop_events(client, wf) if e["event_type"] == "loop.exhausted"][0]["payload"]
    assert "max_iterations_total" in ex["reason"]


async def test_until_error_and_best_by_error_fall_back_to_the_latest(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    loop = {**LOOP, "until": "review_guide.output.rating >= 8", "best_by": "review_guide.output.rating"}
    wf = await start(client, loop, extra_tasks=[task("final_note", "writer", ["review_guide"])])
    await make_engine(llm([review(5)])).run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED"  # never stuck, never an endless loop
    ex = [e for e in await loop_events(client, wf) if e["event_type"] == "loop.exhausted"][0]["payload"]
    assert ex["chosen_iteration"] == 1 and "condition_error" in ex["reason"] and "best_by" in ex["reason"]
