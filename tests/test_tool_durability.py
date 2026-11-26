"""Crash-safe tools: a restart resumes the tool loop from the journal; a write that happened is never repeated;
a call whose outcome is unknown is re-sent only when that is safe (idempotency key), else a human decides."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.researcher import ResearcherAgent
from app.agents.writer import WriterAgent
from app.config import get_settings
from app.core.enums import TaskStatus
from app.db.base import utcnow
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models import Task, TaskAttempt, ToolCall
from app.tools import journal, netguard
from app.tools.base import ToolContext
from app.tools.email import SendEmailArgs, SendEmailTool
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.reliability_helpers import calls_for
from tests.test_budgets import fake_router
from tests.tool_helpers import BASE_AGENTS, BASE_TOOLS, RESEARCH, call, final, tool_rows, use_tools

REPORT = {"title": "EV summary", "content": "Sent.", "word_count": 1}
EMAIL = {"to": ["team@example.com"], "subject": "EV summary", "body": "Sales grew 31%."}
HOOK = {"url": "https://hooks.slack.com/services/T1/B2", "payload": {"text": "EV summary ready"}}


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WriterAgent, "allowed_tools", ("send_email", "webhook_post"))
    monkeypatch.setattr(ResearcherAgent, "allowed_tools", ("web_search", "web_fetch"))
    tools = {**BASE_TOOLS, "send_email": {**BASE_TOOLS["send_email"], "approval": "never"},
             "webhook_post": {**BASE_TOOLS["webhook_post"], "approval": "never"}}
    use_tools(monkeypatch, tools=tools, agents={**BASE_AGENTS, "writer": ["send_email", "webhook_post"]})


async def crashed_mid_loop(client: AsyncClient, sf: async_sessionmaker[AsyncSession], tool: str,
                           args: dict[str, Any], status: str, side_effect: str = "external_write") -> tuple[str, str]:
    """Attempt 1 of task w journaled `tool(args)` with `status`, then the process died (lease still valid)."""
    wf_id = await started_workflow(client, [task("w", "writer")])
    t = (await tasks_by_key(client, wf_id))["w"]
    task_id, now = uuid.UUID(t["id"]), utcnow()
    key = journal.idempotency_key(uuid.UUID(wf_id), "w", tool, journal.args_hash(args))
    async with sf() as s:
        await s.execute(update(Task).where(Task.id == task_id).values(
            status=TaskStatus.RUNNING, attempt_count=1, lease_owner="dead-process",
            lease_expires_at=now + timedelta(seconds=300), started_at=now))
        s.add(TaskAttempt(task_id=task_id, workflow_id=uuid.UUID(wf_id), attempt_number=1,
                          worker_id="dead-process", outcome="running"))
        s.add(ToolCall(workflow_id=uuid.UUID(wf_id), task_id=task_id, attempt_number=1, step_index=0, tool=tool,
                       arguments=args, args_hash=journal.args_hash(args), idempotency_key=key,
                       side_effect=side_effect, status=status, started_at=now,
                       finished_at=now if status == "succeeded" else None,
                       result={"backend": "outbox", "message_id": f"<{key}@agentflow.local>",
                               "recipients": args.get("to"), "subject": args.get("subject")}
                       if status == "succeeded" else None))
        await s.commit()
    return wf_id, key


def outbox(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "outbox").glob("*.eml")) if (tmp_path / "outbox").exists() else []


async def send_like_attempt_one(tmp_path: Path, key: str) -> None:
    """The email really went out in attempt 1 (outbox file written) just before the crash."""
    s = get_settings().model_copy(update={"outbox_dir": tmp_path / "outbox"})
    ctx = ToolContext(workflow_id=uuid.uuid4(), task_key="w", attempt=1, sandbox=tmp_path, idempotency_key=key,
                      settings=s, options={"backend": "outbox", "allowed_recipients": ["*@example.com"]})
    await SendEmailTool().execute(SendEmailArgs(**EMAIL), ctx)


@pytest.mark.parametrize("next_steps", [
    [final(REPORT)],                                  # the model continues from the journal
    [call("send_email", **EMAIL), final(REPORT)],     # the model proposes the same email again
], ids=["continues", "re-proposes"])
async def test_crash_after_email_sent_never_sends_it_twice(client: AsyncClient, make_engine, session_factory,
                                                           tmp_path: Path, next_steps: list) -> None:  # noqa: ANN001
    wf_id, key = await crashed_mid_loop(client, session_factory, "send_email", EMAIL, "succeeded")
    await send_like_attempt_one(tmp_path, key)
    llm = FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=list(next_steps))])
    router, _ = fake_router(session_factory, llm=llm)
    engine = make_engine(router, engine_single_node=True, outbox_dir=tmp_path / "outbox")
    assert (await engine.recover())["reclaimed_tasks"] == 1
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert len(outbox(tmp_path)) == 1  # exactly one email, ever
    assert calls_for(llm, "w") == len(next_steps)  # resumed: only the NEXT step(s) were asked for
    assert "Step 1: send_email" in llm.calls[0]["user_prompt"]  # the transcript came from the journal
    rows = [r for r in await tool_rows(session_factory, wf_id) if r.tool == "send_email"]
    assert rows[0].status == "succeeded" and all(r.reused_from_id == rows[0].id for r in rows[1:])


async def test_unknown_webhook_outcome_is_resent_with_the_same_idempotency_key(
        client: AsyncClient, make_engine, session_factory, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    sent: list[httpx.Request] = []

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["54.1.2.3"]

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    monkeypatch.setattr(netguard, "transport_factory",
                        lambda: httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, text="ok")))
    wf_id, key = await crashed_mid_loop(client, session_factory, "webhook_post", HOOK, "running")
    llm = FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=[final(REPORT)])])
    router, _ = fake_router(session_factory, llm=llm)
    engine = make_engine(router, engine_single_node=True)
    await engine.recover()
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert len(sent) == 1 and sent[0].headers["idempotency-key"] == key  # the receiver can de-duplicate
    assert [r.status for r in await tool_rows(session_factory, wf_id)] == ["unknown_outcome", "succeeded"]


async def test_unknown_smtp_outcome_goes_to_a_human(client: AsyncClient, make_engine, session_factory,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    smtp_email = {**BASE_TOOLS["send_email"], "backend": "smtp", "approval": "never",
                  "smtp": {"host": "smtp.example.com", "port": 587}}
    use_tools(monkeypatch, tools={**BASE_TOOLS, "send_email": smtp_email},
              agents={**BASE_AGENTS, "writer": ["send_email"]})
    wf_id, _ = await crashed_mid_loop(client, session_factory, "send_email", EMAIL, "running")
    llm = FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=[final(REPORT)])])
    router, _ = fake_router(session_factory, llm=llm)
    engine = make_engine(router, engine_single_node=True)
    await engine.recover()
    await engine.run_until_idle()
    assert (await tasks_by_key(client, wf_id))["w"]["status"] == "AWAITING_APPROVAL"
    p = (await client.get(f"/approvals?status=pending&workflow_id={wf_id}")).json()[0]["request_payload"]
    assert "may have sent this" in p["warning"] and p["tool"] == "send_email"
    assert calls_for(llm, "w") == 0  # nothing re-sent and no new model call before the human decides


@pytest.mark.parametrize(("reuse_seconds", "expect"), [(3600, ["succeeded", "succeeded"]), (0, ["succeeded", "succeeded"])])
async def test_read_results_are_reused_only_inside_the_window(client: AsyncClient, make_engine, session_factory,
                                                              reuse_seconds: int, expect: list[str]) -> None:  # noqa: ANN001
    llm = FakeLLMClient(scripts=[FakeScript(match=match("r"), steps=[
        call("web_search", query="india ev"), call("web_search", query="india ev"), final(RESEARCH)])])
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router, tool_result_reuse_seconds=reuse_seconds).run_until_idle()
    rows = await tool_rows(session_factory, wf_id)
    assert [r.status for r in rows] == expect
    reused = [r.reused_from_id is not None for r in rows]
    assert reused == ([False, True] if reuse_seconds else [False, False])
