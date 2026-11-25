"""Pure DAG rules (app.core.dag): no DB, no LLM."""

from __future__ import annotations

import pytest

from app.core.dag import (
    TaskNode,
    execution_levels,
    find_cycle,
    leaves,
    roots,
    to_mermaid,
    topological_order,
    validate_plan,
)

AGENTS = ["analyst", "researcher", "reviewer", "writer", "planner"]  # planner present on purpose


def n(key: str, deps: list[str] | None = None, agent: str = "researcher", **kw: str) -> TaskNode:
    return TaskNode(key=key, agent_type=agent, depends_on=deps or [],
                    name=kw.get("name", key), description=kw.get("description", f"do {key}"))


def codes(tasks: list[TaskNode], max_tasks: int = 12) -> list[str]:
    return [e.code for e in validate_plan(tasks, AGENTS, max_tasks)]


def messages(tasks: list[TaskNode], max_tasks: int = 12) -> list[str]:
    return [e.message for e in validate_plan(tasks, AGENTS, max_tasks)]


LINEAR = [n("a"), n("b", ["a"]), n("c", ["b"])]
DIAMOND = [n("top"), n("left", ["top"]), n("right", ["top"]), n("bottom", ["left", "right"], "writer")]
PARALLEL_ROOTS = [n("r2"), n("r1"), n("join", ["r1", "r2"], "analyst")]


# ---------------------------------------------------------------- valid graphs


@pytest.mark.parametrize("tasks", [LINEAR, DIAMOND, PARALLEL_ROOTS], ids=["linear", "diamond", "parallel-roots"])
def test_valid_graphs(tasks: list[TaskNode]) -> None:
    assert validate_plan(tasks, AGENTS, 12) == []


# ---------------------------------------------------------------- each invalid case


def test_cycle_of_two() -> None:
    assert "Dependency cycle: a -> b -> a" in messages([n("a", ["b"]), n("b", ["a"])])


def test_cycle_of_three_reports_path() -> None:
    tasks = [n("a", ["c"]), n("b", ["a"]), n("c", ["b"]), n("d")]
    assert "Dependency cycle: a -> c -> b -> a" in messages(tasks)


def test_self_dependency() -> None:
    assert codes([n("a", ["a"])]) == ["self_dependency"]


def test_unknown_dependency() -> None:
    assert messages([n("a", ["ghost"])]) == ["Task 'a' depends on unknown task 'ghost'"]


def test_duplicate_dependency_entry() -> None:
    assert codes([n("a"), n("b", ["a", "a"])]) == ["duplicate_dependency"]


def test_duplicate_keys() -> None:
    assert "duplicate_key" in codes([n("a"), n("a")])


@pytest.mark.parametrize("key", ["Research", "1st", "has-dash", "has space", "", "x" * 51, "_lead"])
def test_bad_key_format(key: str) -> None:
    assert "invalid_key" in codes([n(key)])


def test_key_of_50_chars_is_ok() -> None:
    assert codes([n("x" * 50)]) == []


def test_planner_as_agent_type() -> None:
    assert codes([n("p", agent="planner")]) == ["planner_not_allowed"]


def test_unknown_agent_type_lists_valid_types() -> None:
    (msg,) = messages([n("a", agent="wizard")])
    assert "unknown agent_type 'wizard'" in msg
    assert "Valid agent types: analyst, researcher, reviewer, writer" in msg  # no planner


def test_empty_name_and_description() -> None:
    assert codes([n("a", name=" ", description="")]) == ["empty_name", "empty_description"]


def test_too_many_tasks() -> None:
    tasks = [n(f"t{i}") for i in range(4)]
    assert codes(tasks, max_tasks=3) == ["too_many_tasks"]


def test_empty_plan() -> None:
    assert codes([]) == ["empty_plan"]


def test_multiple_errors_reported_together() -> None:
    tasks = [
        n("Bad Key", agent="wizard"),
        n("a", ["a", "ghost"]),
        n("b", ["c"]),
        n("c", ["b"]),
        n("a"),
    ]
    found = set(codes(tasks, max_tasks=4))
    assert found == {"too_many_tasks", "duplicate_key", "invalid_key", "unknown_agent_type",
                     "self_dependency", "unknown_dependency", "cycle"}


# ---------------------------------------------------------------- ordering & rendering


def test_topological_order_is_deterministic() -> None:
    assert topological_order(DIAMOND) == ["top", "left", "right", "bottom"]
    assert topological_order(PARALLEL_ROOTS) == ["r1", "r2", "join"]
    assert topological_order(list(reversed(DIAMOND))) == ["top", "left", "right", "bottom"]


def test_topological_order_rejects_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        topological_order([n("a", ["b"]), n("b", ["a"])])


def test_execution_levels() -> None:
    assert execution_levels(LINEAR) == [["a"], ["b"], ["c"]]
    assert execution_levels(DIAMOND) == [["top"], ["left", "right"], ["bottom"]]
    # A task waits for its deepest dependency.
    tasks = [n("a"), n("b", ["a"]), n("c"), n("d", ["b", "c"])]
    assert execution_levels(tasks) == [["a", "c"], ["b"], ["d"]]


def test_roots_and_leaves() -> None:
    assert roots(DIAMOND) == ["top"]
    assert leaves(DIAMOND) == ["bottom"]
    assert roots(PARALLEL_ROOTS) == ["r2", "r1"]


def test_to_mermaid() -> None:
    assert to_mermaid(DIAMOND) == "\n".join([
        "graph TD",
        '    top["top (researcher)"]',
        '    left["left (researcher)"]',
        '    right["right (researcher)"]',
        '    bottom["bottom (writer)"]',
        "    top --> left",
        "    top --> right",
        "    left --> bottom",
        "    right --> bottom",
    ])


def test_find_cycle_handles_long_chains_without_recursion() -> None:
    chain = {f"t{i}": [f"t{i + 1}"] for i in range(5000)} | {"t5000": []}
    assert find_cycle(chain) is None
    chain["t5000"] = ["t0"]
    assert find_cycle(chain) is not None
