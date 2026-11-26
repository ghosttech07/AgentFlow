"""List the models available to every configured provider, as "provider:model" route entries.

    python scripts/list_provider_models.py

Gemini (models that support generateContent), OpenAI (/v1/models), Anthropic (/v1/models) and local
Ollama (/api/tags). A provider without a key (or an Ollama that is not running) is reported as
skipped. Listing models is free: no tokens are spent. Keys are never printed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402


async def gemini(settings) -> list[str]:  # noqa: ANN001
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    names = []
    async for m in await client.aio.models.list():
        if "generateContent" in (m.supported_actions or []):
            names.append(m.name.removeprefix("models/"))
    return sorted(names)


async def openai_models(settings) -> list[str]:  # noqa: ANN001
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    return sorted([m.id async for m in client.models.list()])


async def anthropic_models(settings) -> list[str]:  # noqa: ANN001
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    return sorted([m.id async for m in client.models.list()])


async def ollama_models(settings) -> list[str]:  # noqa: ANN001
    base = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{base}/api/tags")
        resp.raise_for_status()
    return sorted(m["name"] for m in resp.json().get("models", []))


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = get_settings()
    checks = [
        ("gemini", s.gemini_api_key is not None, "GEMINI_API_KEY not set", gemini),
        ("openai", s.openai_api_key is not None, "OPENAI_API_KEY not set", openai_models),
        ("anthropic", s.anthropic_api_key is not None, "ANTHROPIC_API_KEY not set", anthropic_models),
        ("ollama", True, "", ollama_models),
    ]
    for name, configured, why, fetch in checks:
        if not configured:
            print(f"\n{name}: skipped ({why})")
            continue
        try:
            models = await fetch(s)
        except Exception as exc:  # report and continue with the next provider
            print(f"\n{name}: unavailable ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        print(f"\n{name}: {len(models)} model(s)")
        for m in models:
            print(f"  {name}:{m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
