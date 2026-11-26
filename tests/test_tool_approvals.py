"""Tool-call approvals: suspend mid-loop (slot freed), approve with edited arguments, reject variants,
expiry, cancel. The approved call runs exactly once."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agents.writer import WriterAgent
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models import TaskAttempt
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.test_budgets import fake_router
from tests.tool_helpers import BASE_AGENTS, BASE_TOOLS, call, final, tool_rows, use_tools

REPORT = {"title": "EV summary", "content": "Sent the summary.", "word_count": 3}
EMAIL = {"to": ["team@example.com"], "subject": "EV summary", "body": "Sales grew 31%."}


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WriterAgent, "allowed_tools", ("send_email", "file_write"))
    use_tools(monkeypatch, agents={**BASE_AGENTS, "writer": ["send_email", "file_write"]})


def engine_for(make_engine, session_factory, tmp_path: Path, *steps: Any, key: str = "w", **settings: Any):  # noqa: ANN001, ANN201
    llm = FakeLLMClient(scripts=[FakeScript(match=match(key), steps=list(steps))])
    router, _ = fake_router(session_factory, llm=llm)
    return make_engine(router, outbox_dir=tmp_path / "outbox", **settings), llm


async def pending_tool_approval(client: AsyncClient, wf_id: str) -> dict[str, Any]:
    rows = (await client.get(f"/approvals?status=pending&workflow_id={wf_id}")).json()
    assert len(rows) == 1, rows
    assert rows[0]["request_payload"]["kind"] == "tool_call"
    return rows[0]


def outbox(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "outbox").glob("*.eml")) if (tmp_path / "outbox").exists() else []


async def test_email_suspends_the_task_and_frees_the_worker_slot(client: AsyncClient, make_engine, session_factory,
                                                                  tmp_path: Path) -> None:  # noqa: ANN001
    engine, _ = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT),
                           engine_max_concurrency=1)
    wf_a = await started_workflow(client, [task("w", "writer")])
    wf_b = await started_workflow(client, [task("r", "researcher")])  # no tools: one plain call
    await engine.run_until_idle()
    a, b = await detail(client, wf_a), await detail(client, wf_b)
    assert a["status"] == "AWAITING_APPROVAL" and a["tasks"][0]["status"] == "AWAITING_APPROVAL"
    assert b["status"] == "COMPLETED"  # the only worker slot was not held while waiting for a human
    approval = await pending_tool_approval(client, wf_a)
    p = approval["request_payload"]
    assert p["tool"] == "send_email" and p["arguments"] == EMAIL and p["side_effect"] == "external_write"
    async with session_factory() as s:
        att = await s.scalar(select(TaskAttempt).where(TaskAttempt.task_id == approval["task_id"]))
    assert att.outcome == "suspended" and att.loop_state["pending_call_id"]
    assert outbox(tmp_path) == []
    assert a["tasks"][0]["attempt_count"] < a["tasks"][0]["max_attempts"]  # no retry was used up


async def test_approve_with_edited_arguments_sends_exactly_that_once(client: AsyncClient, make_engine, session_factory,
                                                                       tmp_path: Path) -> None:  # noqa: ANN001
    engine, llm = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT))
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    approval = await pending_tool_approval(client, wf)
    edited = {**EMAIL, "subject": "Weekly EV summary (edited)"}
    resp = await client.post(f"/approvals/{approval['id']}/approve", json={"actor": "dana", "arguments_overrides": edited})
    assert resp.status_code == 200, resp.text
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "COMPLETED"
    files = outbox(tmp_path)
    assert len(files) == 1 and "Subject: Weekly EV summary (edited)" in files[0].read_text()
    rows = [r for r in await tool_rows(session_factory, wf) if r.tool == "send_email"]
    assert len(rows) == 1 and rows[0].status == "succeeded"
    assert rows[0].arguments["subject"] == "Weekly EV summary (edited)" and rows[0].proposed_arguments == EMAIL
    assert "Weekly EV summary (edited)" in llm.calls[-1]["user_prompt"]  # the agent saw what was really sent


async def test_invalid_override_is_422_and_the_approval_stays_pending(client: AsyncClient, make_engine,
                                                                      session_factory, tmp_path: Path) -> None:  # noqa: ANN001
    engine, _ = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT))
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    approval = await pending_tool_approval(client, wf)
    resp = await client.post(f"/approvals/{approval['id']}/approve",
                             json={"actor": "dana", "arguments_overrides": {"to": [], "subject": "x", "body": "b"}})
    assert resp.status_code == 422
    assert (await pending_tool_approval(client, wf))["id"] == approval["id"]


async def test_reject_inform_agent_lets_it_finish_without_sending(client: AsyncClient, make_engine, session_factory,
                                                                  tmp_path: Path) -> None:  # noqa: ANN001
    engine, llm = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT))
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    approval = await pending_tool_approval(client, wf)
    resp = await client.post(f"/approvals/{approval['id']}/reject",
                             json={"actor": "dana", "reason": "not this week", "on_reject": "inform_agent"})
    assert resp.status_code == 200, resp.text
    await engine.run_until_idle()
    assert (await detail(client, wf))["status"] == "COMPLETED" and outbox(tmp_path) == []
    assert "denied by human: not this week" in llm.calls[-1]["user_prompt"]
    assert [r.status for r in await tool_rows(session_factory, wf)] == ["denied"]


async def test_reject_fail_task_fails_the_workflow(client: AsyncClient, make_engine, session_factory,
                                                   tmp_path: Path) -> None:  # noqa: ANN001
    engine, _ = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT))
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    approval = await pending_tool_approval(client, wf)
    bad = await client.post(f"/approvals/{approval['id']}/reject",
                            json={"actor": "dana", "reason": "no", "on_reject": "skip_task"})
    assert bad.status_code == 422  # task-approval options do not apply to a tool call
    resp = await client.post(f"/approvals/{approval['id']}/reject",
                             json={"actor": "dana", "reason": "never", "on_reject": "fail_task"})
    assert resp.status_code == 200
    await engine.run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "FAILED" and d["tasks"][0]["error_type"] == "rejected" and outbox(tmp_path) == []


async def test_tool_approval_expiry_fails_the_task(client: AsyncClient, make_engine, session_factory,
                                                   tmp_path: Path) -> None:  # noqa: ANN001
    engine, _ = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT),
                           approval_timeout_hours=0.0001 / 3600)
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    await engine.run_until_idle()  # next tick sees the approval expired
    d = await detail(client, wf)
    assert d["status"] == "FAILED" and d["tasks"][0]["error_type"] == "approval_expired"
    assert [r.status for r in await tool_rows(session_factory, wf)] == ["denied"]


async def test_cancel_while_awaiting_a_tool_approval(client: AsyncClient, make_engine, session_factory,
                                                     tmp_path: Path) -> None:  # noqa: ANN001
    engine, _ = engine_for(make_engine, session_factory, tmp_path, call("send_email", **EMAIL), final(REPORT))
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    approval = await pending_tool_approval(client, wf)
    assert (await client.post(f"/workflows/{wf}/cancel", json={"actor": "dana"})).status_code == 200
    rows = (await client.get(f"/approvals?workflow_id={wf}")).json()
    assert [(r["id"], r["status"]) for r in rows] == [(approval["id"], "cancelled")]
    assert [r.status for r in await tool_rows(session_factory, wf)] == ["denied"] and outbox(tmp_path) == []


async def test_approval_needed_even_when_config_says_never_after_untrusted_content(
        client: AsyncClient, make_engine, session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    tools = {**BASE_TOOLS, "send_email": {**BASE_TOOLS["send_email"], "approval": "never"}}
    monkeypatch.setattr(WriterAgent, "allowed_tools", ("web_fetch", "send_email"))
    use_tools(monkeypatch, tools=tools, agents={**BASE_AGENTS, "writer": ["web_fetch", "send_email"]})
    engine, _ = engine_for(make_engine, session_factory, tmp_path,
                           call("web_fetch", url="https://news.example.com/india-ev-sales-2026"),
                           call("send_email", **EMAIL), final(REPORT))
    wf = await started_workflow(client, [task("w", "writer")])
    await engine.run_until_idle()
    p = (await pending_tool_approval(client, wf))["request_payload"]
    assert p["tainted"] is True and "https://news.example.com/india-ev-sales-2026" in p["untrusted_sources"]
    rows = await tool_rows(session_factory, wf)
    assert [(r.tool, r.status, r.tainted_context) for r in rows] == [
        ("web_fetch", "succeeded", False), ("send_email", "awaiting_approval", True)]
    assert outbox(tmp_path) == []
    assert (await tasks_by_key(client, wf))["w"]["status"] == "AWAITING_APPROVAL"
