"""LLM client contract shared by the real Gemini client and the test fake."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Source(BaseModel):
    title: str
    url: str


@dataclass
class LLMResponse:
    data: BaseModel  # validated instance of the requested output_model
    raw_text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    sources: list[Source] = field(default_factory=list)


class LLMClient(Protocol):
    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        use_search: bool = False,
        thinking_level: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Return output validated against `output_model`, or raise an app.llm.errors.LLMError.

        thinking_level: "minimal" | "low" | "medium" | "high"; clients may ignore it.
        model: preferred model for this call (e.g. a fast worker model); None = the client default.
        """
        ...
