"""Tool visibility: the per-task journal endpoint, timeline summary + markers, metrics per tool, dashboard."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.engine_helpers import match, started_workflow, task
from tests.test_budgets import fake_router
from tests.tool_helpers import RESEARCH, call, final, use_tools


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    use_tools(monkeypatch)


async def run_research(client: AsyncClient, make_engine, session_factory) -> str:  # noqa: ANN001
    output = {**RESEARCH, "sources": [{"title": "x", "url": "https://news.example.com/india-ev-sales-2026"},
                                      {"title": "y", "url": "https://invented.example.org/a"}]}
    llm = FakeLLMClient(scripts=[FakeScript(match=match("r"), steps=[
        # The evil page comes from the search results, so fetching it is allowed by link provenance (Part 10 0a).
        call("web_search", query="malicious ev page"), call("web_search", query="malicious ev page"),
        call("web_fetch", uri="bad"),
        call("web_fetch", url="https://evil.example/ev-report"), final(output)])])
    router, _ = fake_router(session_factory, llm=llm)
    wf = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router).run_until_idle()
    return wf


async def test_tool_calls_endpoint(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    wf = await run_research(client, make_engine, session_factory)
    rows = (await client.get(f"/workflows/{wf}/tasks/r/tool-calls")).json()
    assert [(r["tool"], r["status"], r["reused"]) for r in rows] == [
        ("web_search", "succeeded", False), ("web_search", "succeeded", True),
        ("web_fetch", "rejected_invalid", False), ("web_fetch", "succeeded", False)]
    assert [r["step_index"] for r in rows] == [0, 1, 2, 3]
    assert rows[3]["tainted_context"] is True and rows[3]["injection_warnings"]
    assert rows[0]["duration_ms"] is not None and rows[0]["result"]["backend"] == "fake"
    assert (await client.get(f"/workflows/{wf}/tasks/nope/tool-calls")).status_code == 404


async def test_timeline_summary_and_markers(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    wf = await run_research(client, make_engine, session_factory)
    tl = (await client.get(f"/workflows/{wf}/timeline")).json()
    s = tl["summary"]
    assert s["tool_calls_by_tool"] == {"web_search": 2, "web_fetch": 1} and s["tool_calls_reused"] == 1
    assert (s["sources_verified"], s["sources_removed"], s["injection_flags"]) == (1, 1, 2)  # snippet + page
    markers = tl["spans"][0]["attempts"][0]["tool_calls"]
    assert [m["tool"] for m in markers] == ["web_search", "web_search", "web_fetch", "web_fetch"]
    texts = [e["text"] for e in tl["entries"]]
    assert any("sources checked by code: 1 verified, 1 removed" in t for t in texts)
    assert any("suspicious text" in t for t in texts)


async def test_metrics_per_tool(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    await run_research(client, make_engine, session_factory)
    tools = (await client.get("/metrics/summary")).json()["tools"]
    assert tools["web_search"]["calls"] == 1 and tools["web_fetch"]["calls"] == 1  # reuse + invalid don't count
    assert tools["web_fetch"]["failures"] == 0 and tools["web_search"]["avg_latency_ms"] is not None


async def test_dashboard_shows_tool_calls(client: AsyncClient) -> None:
    page = (await client.get("/dashboard")).text
    for needle in ("/tool-calls", "reused", "untrusted", "arguments_overrides"):
        assert needle in page
