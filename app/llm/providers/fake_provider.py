"""LLM_PROVIDER=fake behind the router, so routing, usage rows and budgets work in tests and demos."""

from __future__ import annotations

from pydantic import BaseModel

from app.llm.base import LLMClient, LLMResponse


class FakeProvider:
    name = "fake"

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def generate(self, model: str, system_prompt: str, user_prompt: str, output_model: type[BaseModel],
                       *, use_search: bool, thinking_level: str | None) -> LLMResponse:
        response = await self.client.generate_structured(system_prompt, user_prompt, output_model,
                                                         use_search=use_search, thinking_level=thinking_level)
        response.provider = "fake"
        response.cost_usd = 0.0
        return response
