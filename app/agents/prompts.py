"""Shared prompt building for agents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

TRUNCATION_MARKER = " ...[TRUNCATED]"

GROUNDING_RULES = (
    "Rely only on the context provided above (and, if you have a search tool, on what it returns). "
    "If information you need is missing, say so explicitly instead of inventing it."
)


def today() -> str:
    """Agents are told the real date so "current" and "latest" mean now, not the training cutoff."""
    return datetime.now(UTC).strftime("%A %d %B %Y (UTC)")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def fair_truncate(texts: dict[str, str], budget: int) -> tuple[dict[str, str], bool]:
    """Fit texts into `budget` chars total. Short texts stay whole; long ones share the rest equally."""
    if sum(len(t) for t in texts.values()) <= budget:
        return dict(texts), False
    result: dict[str, str] = {}
    remaining_budget, remaining = budget, sorted(texts, key=lambda k: len(texts[k]))
    while remaining:
        share = remaining_budget // len(remaining)
        key = remaining.pop(0)
        text = texts[key]
        if len(text) <= share:
            result[key] = text
        else:
            result[key] = text[: max(share - len(TRUNCATION_MARKER), 0)] + TRUNCATION_MARKER
        remaining_budget -= len(result[key])
    return {k: result[k] for k in texts}, True  # keep original order


def build_task_prompt(
    *,
    workflow_goal: str,
    task_name: str,
    task_description: str,
    task_input: dict[str, Any],
    dependency_outputs: dict[str, dict[str, Any]],
    max_context_chars: int,
) -> str:
    deps, truncated = fair_truncate(
        {key: _dump(out) for key, out in dependency_outputs.items()}, max_context_chars
    )
    if deps:
        previous = "\n\n".join(f"### Task '{key}'\n{text}" for key, text in deps.items())
    else:
        previous = "(none - this task has no upstream results)"
    if truncated:
        previous += (
            "\n\nNOTE: Some previous results were truncated to fit the context limit. "
            "Do not guess at the missing parts."
        )
    return (
        f"## TODAY\n{today()}\n\n"
        f"## OVERALL GOAL\n{workflow_goal}\n\n"
        f"## YOUR TASK\n{task_name}\n{task_description or ''}".rstrip()
        + f"\n\n## TASK INPUT (JSON)\n{_dump(task_input)}\n\n"
        f"## RESULTS FROM PREVIOUS TASKS (JSON per task key)\n{previous}\n\n"
        f"## RULES\n{GROUNDING_RULES}\n"
    )
