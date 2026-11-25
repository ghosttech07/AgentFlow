"""LLM layer. Agents depend only on the LLMClient protocol; the app wires in the Part 8 ModelRouter."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMClient, LLMResponse, Source
from app.llm.gemini_client import GeminiClient


@lru_cache
def get_llm_client() -> LLMClient:
    """Process-wide LLM client: the ModelRouter over every configured provider (FastAPI dependency;
    tests override it with a FakeLLMClient). Its adapters and routes are built on first use, or at
    startup via `await client.ensure_started()` (the app lifespan does this, so a bad models.yaml
    stops the app). LLM_PROVIDER=fake routes every role to the canned demo client.
    """
    from app.db.session import SessionLocal
    from app.llm.router import ModelRouter

    settings = get_settings()
    fake = None
    if settings.llm_provider == "fake":
        from app.llm.demo_fake import build_demo_client

        fake = build_demo_client(settings.fake_llm_delay_seconds)
    return ModelRouter(providers=None, routes=None, pricing=None, settings=settings,
                       session_factory=SessionLocal, fake=fake)


__all__ = ["GeminiClient", "LLMClient", "LLMResponse", "Source", "get_llm_client"]
