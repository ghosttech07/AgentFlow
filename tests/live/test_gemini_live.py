"""Real Gemini calls. Excluded by default; run with: pytest -m live"""

from __future__ import annotations

import pytest

from app.agents import AgentInput, create_agent
from app.config import get_settings
from app.llm.gemini_client import GeminiClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(get_settings().gemini_api_key is None, reason="GEMINI_API_KEY not set"),
]


@pytest.fixture
def llm() -> GeminiClient:
    return GeminiClient(get_settings())


async def test_live_researcher(llm: GeminiClient) -> None:
    result = await create_agent("researcher", llm).run(AgentInput(
        workflow_goal="Brief fact sheet about the Eiffel Tower",
        task_key="research", task_name="Research the Eiffel Tower",
        task_description="Height, year completed, and designer. Keep it short.",
    ))
    print(result.model_dump_json(indent=2)[:1500])
    assert len(result.output["key_findings"]) >= 3
    assert result.input_tokens > 0 and result.output_tokens > 0
    if get_settings().enable_search_grounding:
        assert result.sources, "grounding enabled but no sources returned"


async def test_live_writer(llm: GeminiClient) -> None:
    result = await create_agent("writer", llm).run(AgentInput(
        workflow_goal="Brief fact sheet about the Eiffel Tower",
        task_key="write", task_name="Write a 50-word fact sheet",
        task_input={"max_words": 60},
        dependency_outputs={"research": {"key_findings": [
            "The Eiffel Tower is about 330 m tall.",
            "It was completed in 1889.",
            "Gustave Eiffel's company designed and built it.",
        ]}},
    ))
    print(result.model_dump_json(indent=2))
    assert result.output["content"].strip()
    assert 0 < result.output["word_count"] <= 120
