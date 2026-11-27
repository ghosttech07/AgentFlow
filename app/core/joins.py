"""Join rules (Part 10): what a PENDING task needs from its dependencies before it may run.

    all_succeeded (default, v1 behaviour): every dependency SUCCEEDED; a skipped one skips this task too;
                  a failed one -> wait (the failure rule fails the workflow).
    any_succeeded: once every dependency is finished, at least one SUCCEEDED (others may be skipped/failed).
    all_done:      once every dependency is finished, whatever the outcome.
"""

from __future__ import annotations

from typing import Any, Literal

Decision = Literal["ready", "skip", "wait"]
FINISHED = frozenset({"SUCCEEDED", "SKIPPED", "FAILED", "CANCELLED", "SUPERSEDED"})
NOT_SUCCEEDED_DONE = frozenset({"SKIPPED", "CANCELLED", "SUPERSEDED"})
TOLERANT_JOINS = frozenset({"any_succeeded", "all_done"})


def decide(task: Any) -> tuple[Decision, str | None]:
    """(decision, skip reason). Pure: works on anything with .join and .dependencies[].status."""
    statuses = [str(getattr(d.status, "value", d.status)) for d in task.dependencies]
    join = getattr(task, "join", None) or "all_succeeded"
    if join == "all_succeeded":
        if all(s == "SUCCEEDED" for s in statuses):
            return "ready", None
        if any(s == "FAILED" for s in statuses) or any(s not in FINISHED for s in statuses):
            return "wait", None
        return "skip", "upstream_skipped"
    if any(s not in FINISHED for s in statuses):
        return "wait", None
    if join == "any_succeeded":
        return ("ready", None) if any(s == "SUCCEEDED" for s in statuses) else ("skip", "no_dependency_succeeded")
    return "ready", None  # all_done
