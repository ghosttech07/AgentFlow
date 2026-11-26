"""Live provider checks (Part 8). Each test runs only when its provider is configured.

    pytest -m live_ollama -s      # free, local
    pytest -m live_gemini -s      # ONE small call (free tier is ~20 calls/day)
    pytest -m live_anthropic -s / -m live_openai -s
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel

from app.config import get_settings
from app.llm import get_llm_client
from app.llm.circuit import CircuitBreaker
from app.llm.pricing import Pricing
from app.llm.router import ModelRouter
from app.llm.routes import RouteConfig

OLLAMA_MODEL = "llama3.2:3b"  # fast on a 6 GB laptop GPU; qwen3:4b thinks for 1-2 min per call


class Capital(BaseModel):
    country: str
    capital: str


def _ollama_up() -> bool:
    try:
        base = get_settings().ollama_base_url.rstrip("/")
        return httpx.get(f"{base}/models", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


async def _structured(adapter, model: str) -> None:  # noqa: ANN001
    r = await adapter.generate(model, "Answer in JSON.", "What is the capital of Kenya?", Capital,
                               use_search=False, thinking_level="low")
    print(f"\n{adapter.name}:{model} -> {r.data} in {r.latency_ms} ms, tokens {r.input_tokens}/{r.output_tokens}")
    assert r.data.capital.lower().startswith("nairobi")


@pytest.mark.live
@pytest.mark.live_gemini
@pytest.mark.skipif(get_settings().gemini_api_key is None, reason="GEMINI_API_KEY not set")
async def test_live_gemini_structured_call() -> None:
    from app.llm.providers.gemini import GeminiProvider

    await _structured(GeminiProvider(get_settings()), "gemini-3.5-flash-lite")


@pytest.mark.live
@pytest.mark.live_anthropic
@pytest.mark.skipif(get_settings().anthropic_api_key is None, reason="ANTHROPIC_API_KEY not set")
async def test_live_anthropic_structured_call() -> None:
    import anthropic

    from app.llm.providers.anthropic_provider import AnthropicProvider

    client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key.get_secret_value())
    await _structured(AnthropicProvider(client), "claude-haiku-5-5")


@pytest.mark.live
@pytest.mark.live_openai
@pytest.mark.skipif(get_settings().openai_api_key is None, reason="OPENAI_API_KEY not set")
async def test_live_openai_structured_call() -> None:
    from openai import AsyncOpenAI

    from app.llm.providers.openai_provider import OpenAICompatProvider

    client = AsyncOpenAI(api_key=get_settings().openai_api_key.get_secret_value())
    await _structured(OpenAICompatProvider("openai", client), "gpt-5-mini")  # edit to a model on your account


ollama = [pytest.mark.live, pytest.mark.live_ollama, pytest.mark.skipif(not _ollama_up(), reason="Ollama not running")]


@pytest.mark.parametrize("model", [OLLAMA_MODEL])
@ollama[0]
@ollama[1]
@ollama[2]
async def test_live_ollama_structured_call(model: str) -> None:
    from app.llm.providers.ollama_provider import make_ollama

    await _structured(make_ollama(get_settings()), model)


@ollama[0]
@ollama[1]
@ollama[2]
async def test_live_ollama_full_workflow(app: FastAPI, client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    """Whole pipeline on the local model: plan (max 3 tasks) -> run -> COMPLETED at $0."""
    from app.llm.providers.ollama_provider import make_ollama

    settings = get_settings()
    router = ModelRouter(providers={"ollama": make_ollama(settings)},
                         routes=RouteConfig(routes={"default": [f"ollama:{OLLAMA_MODEL}"]}), pricing=Pricing({}),
                         settings=settings, session_factory=session_factory, breaker=CircuitBreaker(3, 60))
    app.dependency_overrides[get_llm_client] = lambda: router
    resp = await client.post("/workflows", json={
        "name": "ollama live", "goal": "Write a 120-word beginner explainer on what a heat pump is.",
        "run": True, "constraints": {"max_tasks": 3}})
    assert resp.status_code == 202
    await make_engine(router, task_timeout_seconds=600).run_until_idle(timeout=1800)
    wf = (await client.get(f"/workflows/{resp.json()['id']}")).json()
    print("\nplan:", [(t["key"], t["agent_type"]) for t in wf["tasks"]], "status:", wf["status"])
    for t in wf["tasks"]:
        print(f"  {t['key']}: {t['status']} via {(t['metrics'] or {}).get('provider')}:{(t['metrics'] or {}).get('model')}")
    assert wf["status"] == "COMPLETED", wf.get("error")
    assert len(wf["tasks"]) <= 3 and wf["usage"]["cost_usd"] == 0
    await asyncio.sleep(0)
