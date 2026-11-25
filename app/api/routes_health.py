"""Liveness/readiness endpoint that actually pings the database."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.common import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
)
async def health(session: AsyncSession = Depends(get_session)) -> HealthResponse | JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # any connection/driver error means "not healthy"
        # Log only the exception type: driver messages can include connection details.
        logger.error("health.database_unavailable error=%s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})
    return HealthResponse(status="ok", database="ok")
