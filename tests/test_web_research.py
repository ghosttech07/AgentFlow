"""Live web research layer, researcher citations, Quick Ask, worker model. No network: search and
page fetching are monkeypatched."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.agents import AgentInput, create_agent
from app.agents.researcher import ResearchOutput, research_queries
from app.config import get_settings
from app.llm import get_llm_client
from app.llm.fake_client import FakeLLMClient
from app.search import web
from app.services.ask_service import AskOutput
from tests.test_model_fallback import BACKUP, PRIMARY, ModelStub, Out, ok
from tests.test_model_fallback import client as stub_gemini

ROWS = {
    "india ev sales 2026": [
        {"title": "EV sales Sept 2026", "url": "https://a.example/sept#top", "snippet": "snip a"},
        {"title": "Blocked page", "url": "https://blocked.example/x", "snippet": "snip b"},
    ],
    "ev policy india": [
        {"title": "EV sales Sept 2026 (dup)", "url": "https://a.example/sept", "snippet": "dup"},
        {"title": "FAME policy", "url": "https://c.example/fame", "snippet": "snip c"},
    ],
}
PAGES = {"https://a.example/sept#top": "India sold 200,000 EVs in September 2026.",
         "https://c.example/fame": "The FAME scheme was replaced by PM E-DRIVE."}


@pytest.fixture
def fake_web(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    searched: list[str] = []

    async def search(query: str, settings: Any) -> list[dict[str, str]]:
        searched.append(query)
        if query == "boom":
            raise ConnectionError("engine down")
        return ROWS.get(query, [])

    async def fetch(client: Any, url: str, settings: Any) -> str:
        return PAGES.get(url, "")

    monkeypatch.setattr(web, "_search_ddgs", search)
    monkeypatch.setattr(web, "_fetch_text", fetch)
    monkeypatch.setattr(get_settings(), "web_search_provider", "ddgs")
    return searched


async def test_gather_dedups_ranks_readable_pages_and_never_raises(fake_web: list[str]) -> None:
    ctx = await web.gather(["india ev sales 2026", "ev policy india", "boom", "india ev sales 2026"],
                           get_settings().model_copy(update={"web_max_pages": 3}))
    assert fake_web == ["india ev sales 2026", "ev policy india", "boom"]  # duplicate query dropped
    assert [s.url for s in ctx.sources] == ["https://a.example/sept#top", "https://c.example/fame",
                                            "https://blocked.example/x"]  # readable first, dup URL once
    assert [s.n for s in ctx.sources] == [1, 2, 3]
    assert ctx.sources[0].content.startswith("India sold") and ctx.sources[2].content == ""
    assert ctx.errors and "boom" in ctx.errors[0]
    prompt = ctx.as_prompt("today")
    assert "[1] EV sales Sept 2026" in prompt and "snip b" in prompt  # unreadable page falls back to snippet


def test_provider_selection() -> None:
    s = get_settings().model_copy(update={"web_search_provider": "auto", "tavily_api_key": None})
    assert web.provider_name(s) == "ddgs"
    from pydantic import SecretStr
    assert web.provider_name(s.model_copy(update={"tavily_api_key": SecretStr("k")})) == "tavily"
    assert web.provider_name(s.model_copy(update={"web_search_provider": "none"})) == "none"


async def test_researcher_reads_web_and_code_sets_cited_sources(fake_web: list[str],
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "gemini_worker_model", BACKUP)
    llm = FakeLLMClient({ResearchOutput: {
        "summary": "India sold 200,000 EVs in Sept 2026 [1].", "confidence": "high", "gaps": [],
        "key_findings": ["Sales hit 200k [1]", "Policy is PM E-DRIVE [2]", "Growth continues"],
        "sources": [{"title": "made up", "url": "https://hallucinated.example"}]}})
    agent_input = AgentInput(workflow_goal="EV guide", task_key="r", task_name="Research",
                             task_input={"search_queries": ["india ev sales 2026", "ev policy india"]})
    result = await create_agent("researcher", llm).run(agent_input)

    call = llm.calls[0]
    assert "## TODAY" in call["user_prompt"] and "## WEB SOURCES (retrieved" in call["user_prompt"]
    assert "India sold 200,000 EVs in September 2026." in call["user_prompt"]
    assert call["use_search"] is False and call["model"] == BACKUP
    urls = [s["url"] for s in result.output["sources"]]
    assert urls == ["https://a.example/sept#top", "https://c.example/fame"]  # cited ones; hallucination gone


async def test_researcher_without_results_is_honest(fake_web: list[str]) -> None:
    llm = FakeLLMClient({ResearchOutput: {"summary": "s", "key_findings": ["a", "b", "c"], "sources": [],
                                          "confidence": "high", "gaps": []}})
    out = (await create_agent("researcher", llm).run(
        AgentInput(workflow_goal="g", task_key="r", task_name="nothing here"))).output
    assert out["confidence"] == "low" and out["sources"] == []
    assert any("no usable pages" in g for g in out["gaps"])


def test_research_queries_fallback() -> None:
    assert research_queries(AgentInput(workflow_goal="Goal text", task_key="k", task_name="Name",
                                       task_description="Find X. Then Y.")) == ["Find X.", "Goal text"]


async def test_quick_ask_endpoint(app: FastAPI, client: AsyncClient, fake_web: list[str]) -> None:
    llm = FakeLLMClient({AskOutput: {"answer": "About 200k in Sept 2026 [1].", "key_points": ["200k [1]"],
                                     "confidence": "medium", "follow_up_questions": ["What about 2025?"]}})
    app.dependency_overrides[get_llm_client] = lambda: llm
    resp = await client.post("/ask", json={"question": "india ev sales 2026"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"].endswith("[1].") and body["search_provider"] == "ddgs"
    assert [(s["n"], s["cited"]) for s in body["sources"]] == [(1, True), (2, False)]
    assert set(body["timings_ms"]) == {"search", "read_pages", "model", "total"}
    assert "## QUESTION\nindia ev sales 2026" in llm.calls[0]["user_prompt"]
    assert (await client.post("/ask", json={"question": "x"})).status_code == 422


async def test_worker_model_is_tried_first_then_falls_back() -> None:
    from tests.test_errors_classification import OVERLOADED, api_error

    stub = ModelStub(**{PRIMARY.replace(".", "-dot-"): [ok()], BACKUP.replace(".", "-dot-"): [api_error(OVERLOADED)]})
    resp = await stub_gemini(stub).generate_structured("s", "u", Out, model=BACKUP)
    assert stub.calls == [BACKUP, PRIMARY] and resp.model == PRIMARY


def test_cited_numbers_accepts_lists() -> None:
    from app.agents.researcher import cited_numbers

    assert cited_numbers(["a [1, 4] b [2]", "c [3;5]", "year [2026] is not a citation"]) == {1, 2, 3, 4, 5}
