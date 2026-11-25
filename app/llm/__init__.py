"""LLM layer. Agents depend only on the LLMClient protocol; the app wires in Gemini."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMClient, LLMResponse, Source
from app.llm.gemini_client import GeminiClient


@lru_cache
def get_llm_client() -> LLMClient:
    """Process-wide LLM client (FastAPI dependency; tests override it with FakeLLMClient).

    LLM_PROVIDER=fake returns a FakeLLMClient with realistic canned output (demos without quota).
    """
    settings = get_settings()
    if settings.llm_provider == "fake":
        from app.llm.demo_fake import build_demo_client

        return build_demo_client(settings.fake_llm_delay_seconds)
    return GeminiClient(settings)


__all__ = ["GeminiClient", "LLMClient", "LLMResponse", "Source", "get_llm_client"]
