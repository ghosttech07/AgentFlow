"""Plan patches (Part 10): the planner (or a human) proposes changes to a RUNNING graph; code validates them.

A patch may only ADD tasks, CANCEL tasks that never started (PENDING/READY, attempt 0), and REWIRE the dependents of
a FAILED task to a replacement it adds. It can never touch SUCCEEDED, RUNNING or AWAITING_APPROVAL work, and the
resulting graph must pass every DAG rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from app.agents.planner import PlannedTask
from app.core.dag import PlanValidationError, TaskNode, validate_adaptive, validate_plan


class Rewire(BaseModel):
    from_failed: str = Field(description="Key of a FAILED task.")
    to_replacement: str = Field(description="Key of a task added by this patch that takes over its dependents.")


class PlanPatch(BaseModel):
    summary: str = Field(description="One or two sentences: what the patch changes and why.")
    add_tasks: list[PlannedTask] = Field(default_factory=list)
    cancel_tasks: list[str] = Field(default_factory=list, description="Keys of tasks that have NOT started.")
    rewire: list[Rewire] = Field(default_factory=list)


CANCELLABLE = {"PENDING", "READY"}


def validate_patch(patch: PlanPatch, tasks: Sequence[Any], valid_agent_types: Iterable[str], *, max_new_tasks: int,
                   max_tasks: int, max_loop_iterations: int) -> list[PlanValidationError]:
    errors: list[PlanValidationError] = []

    def add(code: str, message: str, key: str | None = None) -> None:
        errors.append(PlanValidationError(code, message, key))

    by_key = {t.key: t for t in tasks}
    status = {t.key: str(getattr(t.status, "value", t.status)) for t in tasks}
    new_keys = [t.key for t in patch.add_tasks]
    if not patch.add_tasks and not patch.cancel_tasks:
        add("empty_patch", "The patch changes nothing: add at least one task")
    if len(patch.add_tasks) > max_new_tasks:
        add("too_many_new_tasks", f"A re-plan may add at most {max_new_tasks} tasks (got {len(patch.add_tasks)})")
    for key in new_keys:
        if key in by_key:
            add("key_exists", f"Task key '{key}' already exists; use a new key", key)
    for key in patch.cancel_tasks:
        if key not in by_key:
            add("cancel_unknown", f"Cannot cancel unknown task '{key}'", key)
        elif status[key] not in CANCELLABLE or (getattr(by_key[key], "attempt_count", 0) or 0) > 0:
            add("cancel_not_allowed", f"Cannot cancel '{key}' ({status[key]}): only tasks that never started "
                "(PENDING/READY) may be cancelled; finished, running or approved work is never undone", key)
        elif getattr(by_key[key], "loop_name", None):
            add("cancel_loop_task", f"Cannot cancel '{key}': it is part of loop '{by_key[key].loop_name}'", key)
    for rw in patch.rewire:
        if rw.from_failed not in by_key:
            add("rewire_unknown", f"Cannot rewire unknown task '{rw.from_failed}'", rw.from_failed)
        elif status[rw.from_failed] != "FAILED":
            add("rewire_not_failed", f"Only a FAILED task's dependents can be rewired ('{rw.from_failed}' is "
                f"{status[rw.from_failed]})", rw.from_failed)
        else:
            started = sorted(t.key for t in tasks if any(d.key == rw.from_failed for d in t.dependencies)
                             and status[t.key] not in CANCELLABLE)
            if started:  # a dependent that already ran (tolerated failure) or runs now must not be re-pointed
                add("rewire_dependent_started", f"Cannot rewire '{rw.from_failed}': its dependents "
                    f"{', '.join(started)} already started; add the replacement as a new task instead", rw.from_failed)
        if rw.to_replacement not in new_keys:
            add("rewire_target", f"Rewire target '{rw.to_replacement}' must be a task added by this patch",
                rw.to_replacement)
    if errors:
        return errors
    # The merged graph must pass every rule: existing tasks (minus cancelled), rewired dependents, new tasks.
    rewired = {rw.from_failed: rw.to_replacement for rw in patch.rewire}
    cancelled = set(patch.cancel_tasks)
    merged = [TaskNode(key=t.key, agent_type=t.agent_type,
                       depends_on=[rewired.get(d.key, d.key) for d in t.dependencies if d.key not in cancelled],
                       name=t.name, description=t.description or t.name)
              for t in tasks if t.key not in cancelled]
    merged += [TaskNode(key=t.key, agent_type=t.agent_type, depends_on=list(t.depends_on), name=t.name,
                        description=t.description) for t in patch.add_tasks]
    for e in validate_plan(merged, valid_agent_types, max_tasks):
        add(e.code, f"After the patch: {e.message}", e.task_key)
    if not errors:
        for e in validate_adaptive(patch.add_tasks, [], max_loop_iterations):
            add(e.code, e.message, e.task_key)
    return errors


def patch_diff(patch: PlanPatch) -> list[str]:
    """Readable lines for the approval card / timeline."""
    lines = [f"+ add {t.key} ({t.agent_type}) after {', '.join(t.depends_on) or 'start'}: {t.name}" for t in patch.add_tasks]
    lines += [f"- cancel {k} (never started)" for k in patch.cancel_tasks]
    lines += [f"~ {rw.from_failed} (failed) is replaced by {rw.to_replacement}" for rw in patch.rewire]
    return lines
