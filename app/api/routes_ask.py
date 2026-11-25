"""Quick Ask endpoint: a fast, cited answer from the live web (no workflow)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.llm import LLMClient, get_llm_client
from app.schemas.common import ErrorResponse
from app.services import ask_service

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=1000)


@router.post("/ask", responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
                                429: {"model": ErrorResponse, "description": "Model quota exhausted"}})
async def ask(
    body: AskRequest, llm: LLMClient = Depends(get_llm_client), settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Search the web, read the top pages and answer with [n] citations (typically 3-10 s).
    Returns answer, key_points, confidence, follow_up_questions, sources, model and timings."""
    return await ask_service.ask(body.question.strip(), llm, settings)
