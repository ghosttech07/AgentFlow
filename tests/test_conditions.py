"""Part 10 condition language: evaluated by code over task outputs (AST whitelist, never eval)."""

from __future__ import annotations

import pytest

from app.core.conditions import ConditionError, ConditionSyntaxError, parse_condition

OUT = {
    "review_guide": {"status": "SUCCEEDED", "output": {"approved": False, "score": 7, "issues": [{"severity": "high"}],
                                                        "summary": "Too SHORT"}},
    "research_china": {"status": "SUCCEEDED", "output": {"confidence": "low", "gaps": ["a", "b", "c", "d"]}},
}


@pytest.mark.parametrize(("expr", "expected"), [
    ("review_guide.output.approved and review_guide.output.score >= 8", False),
    ("not review_guide.output.approved or review_guide.output.score >= 8", True),
    ("research_china.output.confidence == 'low'", True),
    ("len(research_china.output.gaps) > 3", True),
    ("lower(review_guide.output.summary) == 'too short'", True),
    ("review_guide.output.issues[0].severity in ['high', 'medium']", True),
    ("review_guide.output.score * 2 - 4 == 10", True),
    ("max(review_guide.output.score, 9) == 9", True),
    ("review_guide.status == 'SUCCEEDED'", True),
    ("review_guide.output.score != None", True),
])
def test_evaluates(expr: str, expected: bool) -> None:
    ok, values = parse_condition(expr, set(OUT)).evaluate(OUT)
    assert ok is expected
    assert values  # the values used are recorded for the event


def test_values_used_are_recorded() -> None:
    _, values = parse_condition("review_guide.output.score >= 8", set(OUT)).evaluate(OUT)
    assert values == {"review_guide.output.score": 7}


@pytest.mark.parametrize("expr", [
    "review_guide.__class__", "review_guide.output.__dict__", "__import__('os')", "(lambda: 1)()",
    "[x for x in research_china.output.gaps]", "9 ** 9 ** 9", "open('x')", "review_guide.output.score.real",
    "eval('1')", "getattr(review_guide, 'output')", "{'a': 1}", "review_guide.output.score if True else 1",
    "x" * 301, "not " * 21 + "True", "review_guide.output.score := 3",
])
def test_rejects_unsafe_or_oversized(expr: str) -> None:
    with pytest.raises((ConditionSyntaxError, ConditionError)):
        parse_condition(expr, set(OUT)).evaluate(OUT)


def test_unknown_reference_is_a_plan_time_error() -> None:
    with pytest.raises(ConditionSyntaxError, match="unknown task 'review_draft'"):
        parse_condition("review_draft.output.score > 5", {"review_guide"})


def test_missing_field_at_runtime_is_a_condition_error() -> None:
    cond = parse_condition("review_guide.output.rating > 5", set(OUT))
    with pytest.raises(ConditionError, match="review_guide.output.rating"):
        cond.evaluate(OUT)


def test_task_not_finished_is_a_condition_error() -> None:
    with pytest.raises(ConditionError, match="has no output"):
        parse_condition("research_china.output.confidence == 'low'", {"research_china"}).evaluate(
            {"research_china": {"status": "SKIPPED", "output": None}})


def test_refs_are_exposed() -> None:
    assert parse_condition("a.output.x > 1 and b.status == 'SUCCEEDED'", {"a", "b"}).refs == {"a", "b"}
