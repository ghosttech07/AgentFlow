"""Regression tests for the Part 9 whole-branch review (C-1, I-2..I-5, I-10, I-11)."""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from pydantic import BaseModel

from app.agents.researcher import ResearcherAgent
from app.agents.writer import WriterAgent
from app.config import get_settings
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models import Task, TaskAttempt, Workflow
from app.search import web
from app.tools import netguard, registry
from app.tools.base import Tool, ToolContext, ToolError
from app.tools.http_request import HttpRequestArgs, HttpRequestTool
from app.tools.loop import ToolLoop, ToolRuntime
from app.tools.webhook import WebhookArgs, WebhookTool
from tests.engine_helpers import match, started_workflow, task, tasks_by_key
from tests.test_budgets import fake_router
from tests.test_tool_durability import EMAIL, REPORT, crashed_mid_loop
from tests.tool_helpers import BASE_AGENTS, BASE_TOOLS, call, final, tool_rows, use_tools

HOOK = {"url": "https://hooks.slack.com/services/T1/B2", "payload": {"text": "hi"}}


@pytest.fixture
def smtp_sent(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    class FakeSMTP:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        def __enter__(self): return self  # noqa: ANN204, E704
        def __exit__(self, *a: Any) -> None: ...  # noqa: E704
        def starttls(self) -> None: ...  # noqa: E704
        def login(self, *a: Any) -> None: ...  # noqa: E704
        def send_message(self, msg: Any) -> None: sent.append(msg)  # noqa: E704

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return sent


# --------------------------------------------------------------------------- C-1
async def test_approving_an_unknown_outcome_sends_once_and_finishes(client: AsyncClient, make_engine, session_factory,
                                                                    monkeypatch: pytest.MonkeyPatch, smtp_sent) -> None:  # noqa: ANN001
    monkeypatch.setattr(WriterAgent, "allowed_tools", ("send_email",))
    smtp = {**BASE_TOOLS["send_email"], "backend": "smtp", "approval": "never", "smtp": {"host": "smtp.example.com"}}
    use_tools(monkeypatch, tools={**BASE_TOOLS, "send_email": smtp}, agents={**BASE_AGENTS, "writer": ["send_email"]})
    wf_id, _ = await crashed_mid_loop(client, session_factory, "send_email", EMAIL, "running")
    llm = FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=[final(REPORT)])])
    router, _ = fake_router(session_factory, llm=llm)
    engine = make_engine(router, engine_single_node=True)
    await engine.recover()
    await engine.run_until_idle()
    a = (await client.get(f"/approvals?status=pending&workflow_id={wf_id}")).json()[0]
    assert (await client.post(f"/approvals/{a['id']}/approve", json={"actor": "dana"})).status_code == 200
    await engine.run_until_idle()
    assert (await tasks_by_key(client, wf_id))["w"]["status"] == "SUCCEEDED"
    assert len(smtp_sent) == 1  # sent exactly once, and no second approval was requested
    assert len((await client.get(f"/approvals?workflow_id={wf_id}")).json()) == 1
    statuses = [r.status for r in await tool_rows(session_factory, wf_id)]
    assert statuses == ["superseded", "succeeded"]


# --------------------------------------------------------------------------- I-2
async def test_a_timed_out_write_is_an_unknown_outcome_and_is_not_blindly_resent(
        client: AsyncClient, make_engine, session_factory, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(WriterAgent, "allowed_tools", ("webhook_post",))
    hook = {**BASE_TOOLS["webhook_post"], "approval": "never"}
    use_tools(monkeypatch, tools={**BASE_TOOLS, "webhook_post": hook}, agents={**BASE_AGENTS, "writer": ["webhook_post"]})
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ReadTimeout("no answer", request=request)  # the POST went out; the reply never came
        return httpx.Response(200, text="ok")

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["54.1.2.3"]

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    monkeypatch.setattr(netguard, "transport_factory", lambda: httpx.MockTransport(handler))
    llm = FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=[
        call("webhook_post", **HOOK), call("webhook_post", **HOOK), final(REPORT)])])
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("w", "writer")])
    await make_engine(router).run_until_idle()
    rows = await tool_rows(session_factory, wf_id)
    assert [r.status for r in rows] == ["superseded", "succeeded"]
    assert len(sent) == 2 and sent[0].headers["idempotency-key"] == sent[1].headers["idempotency-key"]
    assert "outcome unknown" in llm.calls[1]["user_prompt"]


# --------------------------------------------------------------------------- I-3
class HangArgs(BaseModel):
    x: int = 1


class HangTool(Tool):
    name = "hang_test"
    description = "hangs"
    input_model = HangArgs
    output_model = HangArgs
    side_effect = "external_write"
    timeout_seconds = 30

    async def execute(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        await asyncio.Event().wait()
        return args


async def test_cancel_during_a_tool_call_journals_an_unknown_outcome(session_factory) -> None:  # noqa: ANN001
    from app.tools.config import ToolsConfig

    registry.register_tool(HangTool)
    try:
        wf_id, task_id = uuid.uuid4(), uuid.uuid4()
        async with session_factory() as s:
            s.add(Workflow(id=wf_id, name="w", goal="g", status="RUNNING"))
            await s.flush()
            s.add(Task(id=task_id, workflow_id=wf_id, key="w", name="w", agent_type="writer", status="RUNNING"))
            await s.flush()
            s.add(TaskAttempt(task_id=task_id, workflow_id=wf_id, attempt_number=1, worker_id="x"))
            await s.commit()
        cfg = ToolsConfig.model_validate({"tools": {"hang_test": {"approval": "never"}}, "agents": {}})
        llm = FakeLLMClient(scripts=[FakeScript(match="", steps=[call("hang_test", x=1)])])
        from app.agents import AgentInput, create_agent

        agent = create_agent("writer", llm)
        rt = ToolRuntime(session_factory=session_factory, config=cfg, settings=get_settings(), workflow_id=wf_id,
                         task_id=task_id, task_key="w", attempt_number=1, tools=["hang_test"])
        loop = ToolLoop(agent, AgentInput(workflow_goal="g", task_key="w", task_name="w"), rt)
        running = asyncio.create_task(loop.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if [r.status for r in await tool_rows(session_factory, wf_id)] == ["running"]:
                break
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.2)
        rows = await tool_rows(session_factory, wf_id)
        assert [r.status for r in rows] == ["unknown_outcome"] and "cancelled" in rows[0].error
    finally:
        registry.unregister_tool("hang_test")


# --------------------------------------------------------------------------- I-4
async def test_http_request_does_not_follow_redirects_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.config import CredentialResolver, ToolsConfig

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(302, headers={"location": "https://collector.evil.example/steal"})

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["54.1.2.3"]

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    monkeypatch.setattr(netguard, "transport_factory", lambda: httpx.MockTransport(handler))
    cfg = ToolsConfig.model_validate({"credentials": {"gh": {"env": "GH_T"}}})
    ctx = ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=get_settings().workspaces_dir,
                      idempotency_key="k", settings=get_settings(), options={"allowed_domains": ["api.example.com"]},
                      credentials=CredentialResolver(cfg, {"GH_T": "ghp_SECRET"}))
    with pytest.raises(ToolError, match="redirect"):
        await HttpRequestTool().execute(HttpRequestArgs(url="https://api.example.com/x", credential="gh"), ctx)
    assert len(sent) == 1  # the token never travelled to the redirect target


# --------------------------------------------------------------------------- I-5
async def test_researcher_without_tools_that_read_the_web_is_tainted(client: AsyncClient, make_engine, session_factory,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from app.tools import config as tools_config

    monkeypatch.setattr(tools_config, "get_tools_config", lambda: None)
    monkeypatch.setattr(ResearcherAgent, "_uses_web_layer", lambda self: True)

    async def fake_gather(queries: list[str], settings: Any) -> web.WebContext:
        return web.WebContext(queries=queries, sources=[web.WebSource(n=1, title="t", url="https://x.example/p",
                                                                         snippet="s", content="EV sales grew [1]")])

    monkeypatch.setattr(web, "gather", fake_gather)
    router, _ = fake_router(session_factory, llm=FakeLLMClient())
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router).run_until_idle()
    async with session_factory() as s:
        t = await s.get(Task, uuid.UUID((await tasks_by_key(client, wf_id))["r"]["id"]))
    assert t.status == "SUCCEEDED" and t.tainted is True


# --------------------------------------------------------------------------- I-10
async def test_webhook_allowlist_matches_host_not_string_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=get_settings().workspaces_dir,
                      idempotency_key="k", settings=get_settings(), options={"allowed_urls": ["https://hooks.slack.com"]})
    with pytest.raises(ToolError, match="allowlist"):
        await WebhookTool().execute(WebhookArgs(url="https://hooks.slack.com.evil.example/x", payload={}), ctx)
    with pytest.raises(ToolError, match="allowlist"):
        await WebhookTool().execute(WebhookArgs(url="http://hooks.slack.com/x", payload={}), ctx)  # scheme too


# --------------------------------------------------------------------------- I-11
async def test_dashboard_links_only_allow_http_urls(client: AsyncClient) -> None:
    page = (await client.get("/dashboard")).text
    assert "function safeUrl" in page and "^https?:" in page
    assert 'href="${esc(s.url)}"' not in page
