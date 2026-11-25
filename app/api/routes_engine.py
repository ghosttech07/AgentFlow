"""Engine status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.engine import Engine
from app.engine.deps import get_engine

router = APIRouter(prefix="/engine", tags=["engine"])


@router.get("/status")
async def engine_status(engine: Engine | None = Depends(get_engine)) -> dict[str, Any]:
    if engine is None:
        settings = get_settings()
        return {"enabled": False, "running": False, "llm_provider": settings.llm_provider,
                "single_node": settings.engine_single_node, "worker_id": settings.engine_worker_id,
                "max_concurrency": settings.engine_max_concurrency, "in_flight_task_ids": [],
                "planning_workflow_ids": [], "last_tick_at": None}
    return engine.status()
