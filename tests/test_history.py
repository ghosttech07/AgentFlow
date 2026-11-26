"""Quick Ask answers are saved (asks table) and GET /history lists questions and research, newest first."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from app.llm import get_llm_client
from app.llm.errors import LLMTransientError
from app.llm.fake_client import FakeLLMClient
from app.services.ask_service import AskOutput

ANSWER = {"answer": "About 2.3 lakh EVs were sold [1].", "key_points": ["Two-wheelers lead [1]"],
          "confidence": "medium", "follow_up_questions": ["What about cars?"]}


async def test_ask_is_saved_and_can_be_reopened(app: FastAPI, client: AsyncClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient({AskOutput: ANSWER})
    resp = await client.post("/ask", json={"question": "How many EVs were sold in India last month?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] and body["answer"] == ANSWER["answer"]
    again = await client.get(f"/asks/{body['id']}")
    assert again.status_code == 200
    saved = again.json()
    assert saved["question"] == "How many EVs were sold in India last month?"
    assert saved["status"] == "answered" and saved["result"]["answer"] == ANSWER["answer"]


async def test_failed_ask_is_saved_with_its_error(app: FastAPI, client: AsyncClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(error=LLMTransientError("model busy"))
    resp = await client.post("/ask", json={"question": "Will this fail?"})
    assert resp.status_code == 503
    items = (await client.get("/history")).json()["items"]
    assert items[0]["kind"] == "question" and items[0]["status"] == "failed"
    assert "model busy" in items[0]["error"]


async def test_history_lists_questions_and_research_newest_first(app: FastAPI, client: AsyncClient) -> None:
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient({AskOutput: ANSWER})
    await client.post("/ask", json={"question": "First question?"})
    wf = (await client.post("/workflows", json={"name": "EV report", "goal": "Write an EV report"})).json()
    await client.post("/ask", json={"question": "Second question?"})
    items = (await client.get("/history")).json()["items"]
    assert [(i["kind"], i["title"]) for i in items] == [
        ("question", "Second question?"), ("research", "Write an EV report"), ("question", "First question?")]
    research = items[1]
    assert research["id"] == wf["id"] and research["status"] == "PENDING"
    assert (await client.get("/history?limit=1")).json()["items"][0]["title"] == "Second question?"


async def test_unknown_ask_is_404(client: AsyncClient) -> None:
    resp = await client.get("/asks/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
