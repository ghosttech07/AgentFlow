"""Pure task-graph (DAG) functions: validation, ordering, levels, Mermaid. No DB, no LLM.

Tasks are any objects with `key`, `agent_type` and `depends_on` attributes (`name` and
`description` are also checked by validate_plan): TaskCreate, PlannedTask, or a TaskNode.
An edge "A depends on B" means B must finish before A starts (B -> A in the graph).
"""

from __future__ import annotations

import heapq
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")  # lowercase snake_case, 1-50 chars
PLANNER_AGENT_TYPE = "planner"


class TaskLike(Protocol):
    key: str
    agent_type: str
    depends_on: list[str]


@dataclass(frozen=True)
class TaskNode:
    """Minimal task shape, e.g. built from ORM rows for graph rendering."""

    key: str
    agent_type: str
    depends_on: list[str] = field(default_factory=list)
    name: str = ""
    description: str = ""


@dataclass(frozen=True)
class PlanValidationError:
    code: str
    message: str
    task_key: str | None = None

    def __str__(self) -> str:
        return self.message


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def validate_plan(
    tasks: Sequence[TaskLike], valid_agent_types: Iterable[str], max_tasks: int
) -> list[PlanValidationError]:
    """Return EVERY problem with the plan (empty list = valid)."""
    errors: list[PlanValidationError] = []

    def add(code: str, message: str, key: str | None = None) -> None:
        errors.append(PlanValidationError(code, message, key))

    if not tasks:
        add("empty_plan", "Plan must contain at least 1 task")
        return errors
    if len(tasks) > max_tasks:
        add("too_many_tasks", f"Plan has {len(tasks)} tasks; the maximum is {max_tasks}")

    valid = set(valid_agent_types) - {PLANNER_AGENT_TYPE}
    counts = Counter(t.key for t in tasks)
    for key in sorted(k for k, n in counts.items() if n > 1):
        add("duplicate_key", f"Duplicate task key '{key}' ({counts[key]} times)", key)

    for t in tasks:
        if not KEY_RE.match(t.key):
            add("invalid_key", f"Task key '{t.key}' must be lowercase snake_case "
                "(a-z, 0-9, _), start with a letter, 1-50 chars", t.key)
        if t.agent_type == PLANNER_AGENT_TYPE:
            add("planner_not_allowed", f"Task '{t.key}': agent_type 'planner' cannot be used for a task", t.key)
        elif t.agent_type not in valid:
            add("unknown_agent_type", f"Task '{t.key}': unknown agent_type '{t.agent_type}'. "
                f"Valid agent types: {', '.join(sorted(valid))}", t.key)
        for attr in ("name", "description"):
            if not str(getattr(t, attr, "") or "").strip():
                add(f"empty_{attr}", f"Task '{t.key}': {attr} must not be empty", t.key)
        if t.key in t.depends_on:
            add("self_dependency", f"Task '{t.key}' cannot depend on itself", t.key)
        for dep in sorted(d for d, n in Counter(t.depends_on).items() if n > 1):
            add("duplicate_dependency", f"Task '{t.key}' lists dependency '{dep}' more than once", t.key)
        for dep in sorted({d for d in t.depends_on if d not in counts}):
            add("unknown_dependency", f"Task '{t.key}' depends on unknown task '{dep}'", t.key)

    cycle = find_cycle({t.key: list(t.depends_on) for t in tasks})
    if cycle:
        add("cycle", f"Dependency cycle: {' -> '.join(cycle)}", cycle[0])
    return errors


def find_cycle(depends_on: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle as keys (first == last), or None. Iterative DFS (no recursion limit).

    Self-edges and unknown keys are skipped: validate_plan reports them separately.
    """
    state: dict[str, int] = {}  # absent = unvisited, 1 = on current path, 2 = done
    for root in depends_on:
        if root in state:
            continue
        path, iters = [root], [iter(depends_on[root])]
        state[root] = 1
        while iters:
            node = path[-1]
            dep = next(iters[-1], None)
            if dep is None:
                state[node] = 2
                path.pop()
                iters.pop()
            elif dep == node or dep not in depends_on or state.get(dep) == 2:
                continue
            elif state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            else:
                state[dep] = 1
                path.append(dep)
                iters.append(iter(depends_on[dep]))
    return None


# --------------------------------------------------------------------------------------
# Ordering (inputs must be valid DAGs; a cycle raises ValueError)
# --------------------------------------------------------------------------------------


def topological_order(tasks: Sequence[TaskLike]) -> list[str]:
    """Dependencies before dependents; ties broken alphabetically by key (deterministic)."""
    deps = {t.key: set(t.depends_on) for t in tasks}
    dependents: dict[str, list[str]] = {k: [] for k in deps}
    for key, ds in deps.items():
        for d in ds:
            dependents[d].append(key)
    remaining = {k: len(ds) for k, ds in deps.items()}
    ready = [k for k, n in remaining.items() if n == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        key = heapq.heappop(ready)
        order.append(key)
        for child in dependents[key]:
            remaining[child] -= 1
            if remaining[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(deps):
        raise ValueError("Task graph contains a cycle")
    return order


def execution_levels(tasks: Sequence[TaskLike]) -> list[list[str]]:
    """Level 0 = no dependencies; level N = every dependency in an earlier level.

    Tasks in the same level can run in parallel. Keys within a level are sorted.
    """
    deps = {t.key: list(t.depends_on) for t in tasks}
    level: dict[str, int] = {}
    for key in topological_order(tasks):
        level[key] = 1 + max((level[d] for d in deps[key]), default=-1)
    levels: list[list[str]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
    for key in sorted(level):
        levels[level[key]].append(key)
    return levels


def roots(tasks: Sequence[TaskLike]) -> list[str]:
    """Tasks with no dependencies (input order)."""
    return [t.key for t in tasks if not t.depends_on]


def leaves(tasks: Sequence[TaskLike]) -> list[str]:
    """Tasks nothing depends on (input order); usually the final deliverables."""
    needed = {d for t in tasks for d in t.depends_on}
    return [t.key for t in tasks if t.key not in needed]


def to_mermaid(tasks: Sequence[TaskLike]) -> str:
    """Mermaid flowchart; arrows point from a dependency to the task that needs it."""
    lines = ["graph TD"]
    lines += [f'    {t.key}["{t.key} ({t.agent_type})"]' for t in tasks]
    lines += [f"    {d} --> {t.key}" for t in tasks for d in t.depends_on]
    return "\n".join(lines)
