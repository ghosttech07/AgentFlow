"""Quick Ask endpoint (a fast, cited answer from the live web) + the saved history."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_creator, require_viewer
from app.config import Settings, get_settings
from app.db.session import get_session
from app.llm import LLMClient, get_llm_client
from app.schemas.common import ErrorResponse
from app.services import history_service

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=1000)


@router.post("/ask", dependencies=[Depends(require_creator)], responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
                                429: {"model": ErrorResponse, "description": "Model quota exhausted"}})
async def ask(
    body: AskRequest, llm: LLMClient = Depends(get_llm_client), settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Search the web, read the top pages and answer with [n] citations (typically 3-10 s). The question
    and its answer (or error) are saved to the history. Returns id, answer, key_points, confidence,
    follow_up_questions, sources, model and timings."""
    return await history_service.ask_and_save(session, body.question.strip(), llm, settings)


@router.get("/asks/{ask_id}", dependencies=[Depends(require_viewer)], responses={404: {"model": ErrorResponse}})
async def get_ask(ask_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """A saved question with its answer (or error)."""
    return await history_service.get_ask(session, ask_id)


@router.get("/history", dependencies=[Depends(require_viewer)])
async def history(limit: int = Query(default=50, ge=1, le=200),
                  session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Questions (Quick Ask) and research (workflows), newest first."""
    return {"items": await history_service.list_history(session, limit)}
