"""Provider status and LLM usage metrics (Part 8)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_ops_view, require_viewer
from app.db.session import get_session
from app.llm import LLMClient, get_llm_client
from app.llm.router import ModelRouter
from app.services import provider_service

router = APIRouter(tags=["providers"])


@router.get("/providers", dependencies=[Depends(require_viewer)])
async def providers(llm: LLMClient = Depends(get_llm_client)) -> dict[str, Any]:
    """Configured providers, their route entries, circuit state and today's usage vs quota.
    API keys are never returned: only `key_present`."""
    if not isinstance(llm, ModelRouter):  # e.g. a test double
        return {"providers": [], "routes": {}}
    return await provider_service.providers_status(llm)


@router.get("/metrics/summary", dependencies=[Depends(require_ops_view)])
async def metrics_summary(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Totals (calls, failures, cost, tokens, unpriced calls, fallbacks) and per provider / provider:model
    stats (calls, errors, error rate, average latency, cost) from every recorded LLM call."""
    return await provider_service.metrics_summary(session)
