"""Dashboard (Part 7): one static page that talks to the public API from the browser."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["dashboard"])

PAGE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@router.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(PAGE, media_type="text/html")
