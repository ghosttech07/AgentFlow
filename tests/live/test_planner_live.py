"""Real Gemini planner call. Excluded by default; run with: pytest -m live"""

from __future__ import annotations

import pytest

from app.agents.registry import task_agent_types
from app.config import get_settings
from app.core.dag import leaves, validate_plan
from app.llm.gemini_client import GeminiClient
from app.services.planning_service import plan

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(get_settings().gemini_api_key is None, reason="GEMINI_API_KEY not set"),
]

GOAL = "Create a beginner-friendly report comparing EV adoption in India and China"


async def test_live_plan_is_valid_and_parallel() -> None:
    result = await plan(GeminiClient(get_settings()), GOAL)
    print(f"\nsummary: {result.plan.plan_summary}\nrepairs={result.repair_attempts} "
          f"latency_ms={result.latency_ms} tokens={result.input_tokens}/{result.output_tokens}")
    for s in result.specs:
        print(f"  {s.key:28} {s.agent_type:10} <- {s.depends_on}")
    print(f"levels: {result.execution_levels}\n{result.mermaid}")

    assert validate_plan(result.specs, task_agent_types(), get_settings().max_plan_tasks) == []
    assert any(len(level) >= 2 for level in result.execution_levels), "no parallel level"
    agent_by_key = {s.key: s.agent_type for s in result.specs}
    assert {agent_by_key[k] for k in leaves(result.specs)} <= {"writer", "reviewer"}
