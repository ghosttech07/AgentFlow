from __future__ import annotations

import pytest

from app.agents import AgentInput, create_agent
from app.agents.analyst import AnalysisOutput
from app.agents.planner import Plan
from app.agents.prompts import TRUNCATION_MARKER, build_task_prompt, fair_truncate
from app.agents.researcher import ResearchOutput
from app.agents.reviewer import ReviewOutput
from app.agents.writer import WritingOutput, count_words
from app.config import Settings, get_settings
from app.llm.base import Source
from app.llm.fake_client import FakeLLMClient

INPUT = AgentInput(
    workflow_goal="Write a short report on electric vehicles",
    task_key="write",
    task_name="Write report",
    task_description="Draft a 300-word report",
    task_input={"audience": "executives"},
    dependency_outputs={"research": {"summary": "EV sales grew 25% in 2025"}},
)


@pytest.mark.parametrize(
    ("agent_type", "model"),
    [
        ("researcher", ResearchOutput),
        ("analyst", AnalysisOutput),
        ("writer", WritingOutput),
        ("reviewer", ReviewOutput),
        ("planner", Plan),
    ],
)
async def test_agent_returns_valid_output(agent_type: str, model: type) -> None:
    llm = FakeLLMClient()
    result = await create_agent(agent_type, llm).run(INPUT)

    assert result.agent_type == agent_type
    model.model_validate(result.output)
    assert result.model == "fake-model"
    assert llm.calls[0]["output_model"] is model


def test_prompt_contains_goal_task_input_and_dependencies() -> None:
    llm = FakeLLMClient()
    prompt = create_agent("writer", llm).build_user_prompt(INPUT)
    for fragment in (
        "## OVERALL GOAL\nWrite a short report on electric vehicles",
        "## YOUR TASK\nWrite report\nDraft a 300-word report",
        '"audience": "executives"',
        "### Task 'research'",
        "EV sales grew 25% in 2025",
        "instead of inventing it",
    ):
        assert fragment in prompt


@pytest.mark.parametrize("grounding", [True, False])
async def test_only_researcher_uses_search(grounding: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    # Grounding is off by default (no quota on the current key); the flag gates the researcher.
    assert Settings.model_fields["enable_search_grounding"].default is False
    monkeypatch.setattr(get_settings(), "enable_search_grounding", grounding)
    for agent_type, expected in [("researcher", grounding), ("writer", False)]:
        llm = FakeLLMClient()
        await create_agent(agent_type, llm).run(INPUT)
        assert llm.calls[0]["use_search"] is expected


def test_fair_truncation_keeps_short_texts_and_splits_the_rest() -> None:
    texts = {"short": "s" * 10, "big1": "a" * 1000, "big2": "b" * 500}
    out, truncated = fair_truncate(texts, 310)
    assert truncated
    assert out["short"] == "s" * 10  # fits in its share, untouched
    assert out["big1"].endswith(TRUNCATION_MARKER) and out["big2"].endswith(TRUNCATION_MARKER)
    assert abs(len(out["big1"]) - len(out["big2"])) <= 1  # equal shares
    assert sum(map(len, out.values())) <= 310
    assert list(out) == list(texts)  # original order


def test_prompt_notes_truncation() -> None:
    prompt = build_task_prompt(
        workflow_goal="g", task_name="t", task_description="", task_input={},
        dependency_outputs={"a": {"x": "y" * 500}, "b": {"x": "z" * 500}},
        max_context_chars=200,
    )
    assert TRUNCATION_MARKER in prompt and "were truncated" in prompt
    untruncated = build_task_prompt(
        workflow_goal="g", task_name="t", task_description="", task_input={},
        dependency_outputs={"a": {"x": "y"}}, max_context_chars=200,
    )
    assert "truncated" not in untruncated.lower()


async def test_writer_word_count_is_computed_in_code() -> None:
    content = "# EVs\n\nElectric vehicles aren't new - they're growing fast."
    llm = FakeLLMClient({WritingOutput: {"title": "EVs", "content": content, "word_count": 999}})
    result = await create_agent("writer", llm).run(INPUT)
    assert result.output["word_count"] == count_words(content) == 8


async def test_reviewer_cannot_approve_with_high_severity_issue() -> None:
    llm = FakeLLMClient({ReviewOutput: {
        "approved": True, "score": 7, "suggestions": [], "summary": "ok",
        "issues": [{"severity": "high", "description": "unsupported claim"}],
    }})
    result = await create_agent("reviewer", llm).run(INPUT)
    assert result.output["approved"] is False


async def test_researcher_sources_come_from_grounding() -> None:
    grounded = [Source(title="iea.org", url="https://iea.org/ev")]
    llm = FakeLLMClient(sources=grounded)
    result = await create_agent("researcher", llm).run(INPUT)
    assert result.output["sources"] == [{"title": "iea.org", "url": "https://iea.org/ev"}]
    assert result.sources == grounded
