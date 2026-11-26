"""Build the provider adapters once per process. A provider without credentials (or a local Ollama
that does not answer) is not registered; one INFO line says so. Keys are never logged."""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.llm.base import LLMClient
from app.llm.providers.base import ProviderAdapter

logger = logging.getLogger(__name__)

PROVIDERS = ("gemini", "anthropic", "openai", "ollama")


async def ollama_reachable(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.get(f"{base_url.rstrip('/')}/models")).status_code == 200
    except httpx.HTTPError:
        return False


async def build_providers(settings: Settings, *, fake: LLMClient | None = None) -> dict[str, ProviderAdapter]:
    if fake is not None:  # LLM_PROVIDER=fake overrides everything (tests, demos)
        from app.llm.providers.fake_provider import FakeProvider

        return {"fake": FakeProvider(fake)}
    providers: dict[str, ProviderAdapter] = {}
    if settings.gemini_api_key is not None:
        from app.llm.providers.gemini import GeminiProvider

        providers["gemini"] = GeminiProvider(settings)
    else:
        logger.info("provider.skipped name=gemini reason=GEMINI_API_KEY not set")
    if settings.anthropic_api_key is not None:
        import anthropic

        from app.llm.providers.anthropic_provider import AnthropicProvider

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), max_retries=0)
        providers["anthropic"] = AnthropicProvider(client, timeout=settings.llm_timeout_seconds)
    else:
        logger.info("provider.skipped name=anthropic reason=ANTHROPIC_API_KEY not set")
    if settings.openai_api_key is not None:
        from openai import AsyncOpenAI

        from app.llm.providers.openai_provider import OpenAICompatProvider

        client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=0)
        providers["openai"] = OpenAICompatProvider("openai", client, timeout=settings.llm_timeout_seconds)
    else:
        logger.info("provider.skipped name=openai reason=OPENAI_API_KEY not set")
    if await ollama_reachable(settings.ollama_base_url):
        from app.llm.providers.ollama_provider import make_ollama

        providers["ollama"] = make_ollama(settings)
    else:
        logger.info("provider.skipped name=ollama reason=not reachable at OLLAMA_BASE_URL")
    return providers
