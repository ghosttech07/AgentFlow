"""Shared response shapes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    """Every error response looks like `{"error": {"code": "...", "message": "..."}}`."""

    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    database: str
