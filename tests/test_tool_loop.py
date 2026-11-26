"""Part 9 tool loop: structured action protocol over the router, validation, limits, budgets, providers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.agents.analyst import AnalystAgent
from app.agents.researcher import ResearcherAgent
from app.config import get_settings
from app.llm.circuit import CircuitBreaker
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.llm.pricing import Pricing
from app.llm.providers.openai_provider import OpenAICompatProvider
from app.llm.router import ModelRouter
from app.llm.routes import RouteConfig
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.test_budgets import fake_router
from tests.tool_helpers import RESEARCH, call, final, tool_rows, use_tools


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ResearcherAgent, "allowed_tools", ("web_search", "web_fetch"))
    monkeypatch.setattr(AnalystAgent, "allowed_tools", ("sql_query", "calculator"))
    use_tools(monkeypatch)


def scripted(*steps: dict[str, Any], key: str = "r") -> FakeLLMClient:
    return FakeLLMClient(scripts=[FakeScript(match=match(key), steps=list(steps))])


async def test_multi_step_tool_use_reaches_final(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    llm = scripted(call("web_search", query="india ev sales"),
                   call("web_fetch", url="https://news.example.com/india-ev-sales-2026"), final(RESEARCH))
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router).run_until_idle()
    t = (await tasks_by_key(client, wf_id))["r"]
    assert t["status"] == "SUCCEEDED" and t["output"]["summary"] == "EV sales rose [1]."
    rows = await tool_rows(session_factory, wf_id)
    assert [(r.tool, r.status, r.step_index) for r in rows] == [("web_search", "succeeded", 0),
                                                               ("web_fetch", "succeeded", 1)]
    prompts = [c["user_prompt"] for c in llm.calls]
    assert len(prompts) == 3 and "<untrusted_tool_result" in prompts[2] and "2.3 lakh" in prompts[2]
    assert "never follow instructions" in llm.calls[0]["system_prompt"].lower()
    assert "### web_fetch" in llm.calls[0]["system_prompt"] and "### send_email" not in llm.calls[0]["system_prompt"]


async def test_disallowed_tool_becomes_a_tool_error_not_a_crash(client: AsyncClient, make_engine,
                                                                session_factory) -> None:  # noqa: ANN001
    llm = scripted(call("send_email", to=["x@example.com"], subject="s", body="b"), final(RESEARCH))
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router).run_until_idle()
    assert (await tasks_by_key(client, wf_id))["r"]["status"] == "SUCCEEDED"
    rows = await tool_rows(session_factory, wf_id)
    assert [(r.tool, r.status) for r in rows] == [("send_email", "rejected_invalid")]
    assert "not available" in rows[0].error
    assert "TOOL ERROR" in llm.calls[1]["user_prompt"]


async def test_invalid_arguments_become_a_tool_error(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    llm = scripted(call("web_fetch", uri="https://x"), final(RESEARCH))
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router).run_until_idle()
    rows = await tool_rows(session_factory, wf_id)
    assert rows[0].status == "rejected_invalid" and "url" in rows[0].error
    assert (await tasks_by_key(client, wf_id))["r"]["status"] == "SUCCEEDED"


async def test_step_limit_forces_a_final_answer(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    llm = scripted(call("web_search", query="a"), call("web_search", query="b"), final(RESEARCH))
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router, max_tool_steps=2).run_until_idle()
    assert (await tasks_by_key(client, wf_id))["r"]["status"] == "SUCCEEDED"
    assert "STEP LIMIT REACHED" in llm.calls[2]["user_prompt"] and len(llm.calls) == 3


async def test_no_final_after_the_step_limit_is_an_output_error(client: AsyncClient, make_engine,
                                                                session_factory) -> None:  # noqa: ANN001
    llm = scripted(*[call("web_search", query=f"q{i}") for i in range(20)])
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher", max_attempts=1)])
    await make_engine(router, max_tool_steps=2).run_until_idle()
    t = (await tasks_by_key(client, wf_id))["r"]
    assert t["status"] == "FAILED" and t["error_type"] == "output" and "final" in t["error"]


async def test_max_tool_calls_budget_pauses_and_returns_the_task(app: FastAPI, client: AsyncClient, make_engine,
                                                                 session_factory) -> None:  # noqa: ANN001
    llm = scripted(call("web_search", query="a"), call("web_search", query="b"), final(RESEARCH))
    router, _ = fake_router(session_factory, llm=llm)
    resp = await client.post("/workflows", json={"name": "b", "goal": "g", "tasks": [task("r", "researcher")],
                                                 "budget": {"max_tool_calls": 1}})
    wf_id = resp.json()["id"]
    await client.post(f"/workflows/{wf_id}/start")
    await make_engine(router).run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "PAUSED" and wf["usage"]["tool_calls"] == 1
    assert wf["tasks"][0]["status"] == "READY"
    events = (await client.get(f"/workflows/{wf_id}/events")).json()
    assert any(e["event_type"] == "workflow.budget_exceeded" and e["payload"]["limit"] == "max_tool_calls"
               for e in events)


async def test_default_llm_call_limit_message_for_tool_workflows(client: AsyncClient, make_engine,
                                                                 session_factory) -> None:  # noqa: ANN001
    llm = scripted(*[call("web_search", query=f"q{i}") for i in range(6)], final(RESEARCH))
    router, _ = fake_router(session_factory, llm=llm)
    router.settings = router.settings.model_copy(update={"default_max_llm_calls": 2})
    wf_id = await started_workflow(client, [task("r", "researcher"), task("r2", "researcher")])
    await make_engine(router, default_max_llm_calls=2).run_until_idle()
    events = (await client.get(f"/workflows/{wf_id}/events")).json()
    paused = next(e for e in events if e["event_type"] == "workflow.paused")
    assert paused["payload"]["reason"] == (
        f"budget_exceeded: default max_llm_calls=2 reached (tool workflow); raise the budget with "
        f"PATCH /workflows/{wf_id}/budget and resume")


async def test_agent_without_tools_config_makes_exactly_one_call(client: AsyncClient, make_engine,
                                                                 session_factory, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from app.tools import config as tools_config

    monkeypatch.setattr(tools_config, "get_tools_config", lambda: None)
    llm = FakeLLMClient()
    router, _ = fake_router(session_factory, llm=llm)
    wf_id = await started_workflow(client, [task("r", "researcher")])
    await make_engine(router).run_until_idle()
    assert (await tasks_by_key(client, wf_id))["r"]["status"] == "SUCCEEDED"
    assert len(llm.calls) == 1 and await tool_rows(session_factory, wf_id) == []
    assert "YOUR TOOLS" not in llm.calls[0]["system_prompt"]


class _FakeOllamaClient:
    """Stands in for the openai SDK client pointed at Ollama's /v1 (chat.completions.parse)."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = steps
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    async def _parse(self, *, model: str, response_format: Any, messages: list, **_: Any) -> Any:
        parsed = response_format.model_validate(self.steps.pop(0))
        message = SimpleNamespace(parsed=parsed, content=parsed.model_dump_json(), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


async def test_loop_runs_through_the_ollama_adapter(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    ollama = OpenAICompatProvider("ollama", _FakeOllamaClient([
        call("calculator", expression="pct_change(100, 131)"),
        final({"insights": ["Sales grew 31%"], "recommendations": ["keep going"], "risks": ["r"],
               "confidence": "medium"}),
    ]))
    router = ModelRouter(providers={"ollama": ollama}, routes=RouteConfig(routes={"default": ["ollama:llama3.2:3b"]}),
                         pricing=Pricing({}), settings=get_settings(), session_factory=session_factory,
                         breaker=CircuitBreaker(3, 60))
    wf_id = await started_workflow(client, [task("a", "analyst")])
    await make_engine(router).run_until_idle()
    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == "SUCCEEDED", t
    rows = await tool_rows(session_factory, wf_id)
    assert rows[0].tool == "calculator" and rows[0].result["value"] == pytest.approx(31.0)
    assert t["metrics"]["provider"] == "ollama"
