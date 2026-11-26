from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import get_settings
from app.llm import get_llm_client
from app.llm.errors import LLMOutputError, LLMPermanentError, LLMTransientError
from app.llm.fake_client import FakeLLMClient

BODY = {
    "workflow_goal": "Short EV report",
    "task_name": "Research EV market",
    "task_input": {"region": "EU"},
}


async def test_list_agents(client: AsyncClient) -> None:
    resp = await client.get("/agents")
    assert resp.status_code == 200
    agents = {a["agent_type"]: a for a in resp.json()}
    assert set(agents) == {"analyst", "communicator", "planner", "publisher", "researcher", "reviewer", "writer"}
    assert agents["publisher"]["sensitive"] is True
    assert agents["planner"]["assignable"] is False and agents["writer"]["assignable"] is True
    assert "approved" in agents["reviewer"]["output_schema"]["properties"]


async def test_run_agent_with_fake_llm(client: AsyncClient, fake_llm: FakeLLMClient) -> None:
    resp = await client.post("/agents/researcher/run", json=BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_type"] == "researcher"
    assert len(body["output"]["key_findings"]) >= 3
    assert '"region": "EU"' in fake_llm.calls[0]["user_prompt"]


async def test_run_unknown_agent_404(client: AsyncClient) -> None:
    resp = await client.post("/agents/wizard/run", json=BODY)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_agent"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LLMTransientError("Gemini request timed out"), 503, "llm_unavailable"),
        (LLMPermanentError("Gemini API error 401"), 502, "llm_request_failed"),
        (LLMOutputError("Gemini output failed validation"), 502, "llm_invalid_output"),
    ],
)
async def test_llm_errors_map_to_http(
    app: FastAPI, client: AsyncClient, error: Exception, status: int, code: str
) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(error=error)
    resp = await client.post("/agents/writer/run", json=BODY)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    assert resp.json()["error"]["message"] == str(error)


async def test_run_disabled_in_production(app: FastAPI, client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"app_env": "production"}
    )
    resp = await client.post("/agents/researcher/run", json=BODY)
    assert resp.status_code == 404
    assert "disabled in production" in resp.json()["error"]["message"]


async def test_workflow_rejects_unknown_agent_type(client: AsyncClient) -> None:
    resp = await client.post("/workflows", json={
        "name": "wf", "goal": "g",
        "tasks": [{"key": "a", "name": "a", "description": "d", "agent_type": "wizard"}],
    })
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert "unknown agent_type 'wizard'" in message
    assert "Valid agent types: analyst, communicator, publisher, researcher, reviewer, writer" in message
