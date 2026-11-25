"""FastAPI dependency for the running engine (None when ENGINE_ENABLED=false)."""

from __future__ import annotations

from fastapi import Request

from app.engine.engine import Engine


def get_engine(request: Request) -> Engine | None:
    return getattr(request.app.state, "engine", None)
