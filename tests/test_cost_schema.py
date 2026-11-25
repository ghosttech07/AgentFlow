"""Migration 0005 schema: llm_calls, provider_usage, workflows.budget, task_attempts cost/provider."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.models import LLMCall, ProviderUsage, Workflow


async def test_llm_call_rows_and_workflow_budget(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        wf = Workflow(id=uuid.uuid4(), name="w", goal="g", budget={"max_llm_calls": 3})
        s.add(wf)
        await s.flush()
        s.add(LLMCall(workflow_id=wf.id, role="planner", provider="gemini", model="gemini-3.5-flash",
                      outcome="succeeded", input_tokens=100, output_tokens=50, cost_usd=0.000123,
                      latency_ms=900, route_position=0))
        s.add(LLMCall(workflow_id=None, role="ask", provider="ollama", model="qwen3:4b", outcome="failed",
                      error_type="overloaded", route_position=1, fallback_from="gemini:gemini-3.5-flash"))
        await s.commit()
        rows = (await s.scalars(select(LLMCall).order_by(LLMCall.id))).all()
        assert [r.cost_usd for r in rows] == [pytest.approx(0.000123), None]
        assert rows[1].workflow_id is None and rows[1].fallback_from == "gemini:gemini-3.5-flash"
        assert (await s.get(Workflow, wf.id)).budget == {"max_llm_calls": 3}


async def test_provider_usage_window_is_unique(session_factory: async_sessionmaker[AsyncSession]) -> None:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_factory() as s:
        s.add(ProviderUsage(provider="gemini", model="m", window_kind="day", window_start=start, request_count=1))
        await s.commit()
        s.add(ProviderUsage(provider="gemini", model="m", window_kind="day", window_start=start, request_count=1))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_attempts_have_cost_and_provider_columns(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        cols = (await s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'task_attempts'"))).scalars().all()
    assert {"cost_usd", "provider"} <= set(cols)
