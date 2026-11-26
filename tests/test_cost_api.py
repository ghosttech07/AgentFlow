"""Part 8 API: /providers, /metrics/summary, timeline cost-by-provider, budget detail fields."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr

from app.config import get_settings
from app.llm import get_llm_client
from app.llm.circuit import CircuitBreaker
from app.llm.errors import LLMOverloadedError
from app.llm.pricing import Pricing
from app.llm.router import ModelRouter
from app.llm.routes import QuotaSpec, RouteConfig
from tests.engine_helpers import started_workflow, task
from tests.test_router import Out, StubProvider

from app.llm.fake_client import fake_data


class AgentStub(StubProvider):
    """StubProvider that answers any output schema with valid fake data (for real agent tasks)."""

    async def generate(self, model, system_prompt, user_prompt, output_model, *, use_search, thinking_level):  # noqa: ANN001, ANN201
        resp = await super().generate(model, system_prompt, user_prompt, Out, use_search=use_search,
                                      thinking_level=thinking_level)
        resp.data = output_model.model_validate(fake_data(output_model)) if output_model is not Out else resp.data
        return resp

SECRET = "sk-test-SECRET-value"


def stub_router(session_factory, **providers: StubProvider) -> ModelRouter:  # noqa: ANN001
    settings = get_settings().model_copy(update={"gemini_api_key": SecretStr(SECRET), "anthropic_api_key": None,
                                                 "openai_api_key": None})
    routes = RouteConfig(routes={"default": ["gemini:g", "anthropic:c"], "planner": ["gemini:g"]},
                         quotas={"gemini:g": QuotaSpec(requests_per_day=20, timezone="America/Los_Angeles")})
    return ModelRouter(providers=providers, routes=routes, pricing=Pricing({"gemini:g": (1.0, 2.0)}),
                       settings=settings, session_factory=session_factory, breaker=CircuitBreaker(1, 60))


async def test_providers_endpoint_shows_status_never_keys(app: FastAPI, client: AsyncClient, session_factory) -> None:  # noqa: ANN001
    g = StubProvider("gemini", g=[LLMOverloadedError("503")])
    c = StubProvider("anthropic", c=["ok"])
    router = stub_router(session_factory, gemini=g, anthropic=c)
    app.dependency_overrides[get_llm_client] = lambda: router
    await router.generate_structured("s", "u", Out)  # gemini overloads (breaker threshold 1 -> open)

    resp = await client.get("/providers")
    assert resp.status_code == 200 and SECRET not in resp.text
    body = resp.json()
    by_name = {p["name"]: p for p in body["providers"]}
    assert by_name["gemini"]["configured"] is True and by_name["gemini"]["key_present"] is True
    assert by_name["openai"]["configured"] is False and by_name["openai"]["key_present"] is False
    entry = by_name["gemini"]["entries"][0]
    assert entry["entry"] == "gemini:g" and entry["circuit"] == "open"
    assert entry["quota"]["day"]["count"] == 1 and entry["quota"]["day"]["limit"] == 20
    assert entry["quota"]["day"]["timezone"] == "America/Los_Angeles"
    assert body["routes"]["planner"] == ["gemini:g"]


async def test_metrics_summary_per_provider(app: FastAPI, client: AsyncClient, session_factory) -> None:  # noqa: ANN001
    g = StubProvider("gemini", g=[LLMOverloadedError("503"), "fine"])
    router = stub_router(session_factory, gemini=g, anthropic=StubProvider("anthropic", c=["ok"]))
    await router.generate_structured("s", "u", Out)   # gemini fails, anthropic answers (unpriced)
    router.breaker = CircuitBreaker(5, 60)
    await router.generate_structured("s", "u", Out)   # gemini answers: 1000 in / 500 out at $1/$2
    app.dependency_overrides[get_llm_client] = lambda: router
    m = (await client.get("/metrics/summary")).json()
    assert m["totals"]["calls"] == 3 and m["totals"]["failed"] == 1
    assert m["totals"]["cost_usd"] == 0.002 and m["totals"]["unpriced_calls"] == 1
    gem = m["by_entry"]["gemini:g"]
    assert (gem["calls"], gem["errors"], gem["error_rate"]) == (2, 1, 0.5)
    assert m["by_provider"]["anthropic"]["calls"] == 1 and m["fallbacks"] == 1


async def test_timeline_has_cost_by_provider_and_fallbacks(app: FastAPI, client: AsyncClient, make_engine,
                                                           session_factory) -> None:  # noqa: ANN001
    g = AgentStub("gemini", g=[LLMOverloadedError("503"), "a"])
    router = stub_router(session_factory, gemini=g, anthropic=AgentStub("anthropic", c=["b"]))
    wf_id = await started_workflow(client, [task("only")])
    await make_engine(router).run_until_idle()
    tl = (await client.get(f"/workflows/{wf_id}/timeline")).json()
    s = tl["summary"]
    assert s["llm_calls"] == 2 and s["fallbacks"] == 1
    assert s["cost_by_provider"] == {"gemini": 0.0, "anthropic": None}  # anthropic price unknown -> null
    span = tl["spans"][0]
    assert span["fallback_from"] == "gemini:g"
    assert span["attempts"][0]["provider"] == "anthropic" and span["attempts"][0]["model"] == "c"
    texts = [e["text"] for e in tl["entries"]]
    assert any("via anthropic:c" in t and "fallback from gemini:g" in t for t in texts)


async def test_workflow_detail_shows_budget_usage_remaining(client: AsyncClient) -> None:
    wf_id = (await client.post("/workflows", json={"name": "b", "goal": "g", "budget": {"max_cost_usd": 1.5}})).json()["id"]
    d = (await client.get(f"/workflows/{wf_id}")).json()
    assert d["budget"] == {"max_cost_usd": 1.5}
    assert d["effective_budget"]["max_cost_usd"] == 1.5 and d["effective_budget"]["max_llm_calls"] == 50
    assert d["usage"]["llm_calls"] == 0 and d["remaining"] == {"llm_calls": 50, "tokens": None, "cost_usd": 1.5,
                                                               "duration_seconds": None, "tool_calls": None}
