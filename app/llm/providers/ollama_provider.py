"""Local Ollama (free) through its OpenAI-compatible endpoint (OLLAMA_BASE_URL, default
http://localhost:11434/v1) with the openai SDK: one adapter for OpenAI and Ollama, and Ollama
honours `response_format` JSON schemas there (verified live in Part 8). Cost is always 0."""

from __future__ import annotations

from openai import AsyncOpenAI

from app.config import Settings
from app.llm.providers.openai_provider import OpenAICompatProvider


def make_ollama(settings: Settings) -> OpenAICompatProvider:
    # Ollama ignores the key but the SDK requires one.
    client = AsyncOpenAI(base_url=settings.ollama_base_url, api_key="ollama", max_retries=0)
    return OpenAICompatProvider("ollama", client, timeout=settings.llm_timeout_seconds)
