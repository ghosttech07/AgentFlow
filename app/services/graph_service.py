"""All dynamic graph state and changes (Part 10): the adaptive setup of a new plan, loop expansion, plan patches.

Every change runs inside the caller's locked transaction and records events, so a crash can never leave a
half-changed graph. Loop state lives in workflow.context["loops"]; the plan history in plan_versions.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.adaptive_spec import LoopSpec
from app.models.adaptive import PlanVersion
from app.models.task import Task
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)


def loop_state(spec: LoopSpec) -> dict[str, Any]:
    return {"spec": spec.model_dump(by_alias=True), "iteration": 1, "status": "open", "chosen_iteration": None,
            "extra_iterations": 0}


async def init_adaptive(session: AsyncSession, workflow: Workflow, tasks: Sequence[Task], loops: Sequence[LoopSpec],
                        *, replan_on_failure: bool, created_by: str, plan: dict[str, Any],
                        memory: dict[str, bool] | None = None) -> None:
    """Mark loop body tasks (iteration 1), store loop + adaptive settings, and plan version 1. No commit."""
    by_key = {t.key: t for t in tasks}
    for spec in loops:
        for key in spec.body:
            by_key[key].loop_name, by_key[key].iteration, by_key[key].base_key = spec.name, 1, key
    ctx = dict(workflow.context or {})
    if loops:
        ctx["loops"] = {spec.name: loop_state(spec) for spec in loops}
    if replan_on_failure:  # plans without adaptive features keep exactly the old context (backward compatible)
        ctx["adaptive"] = {**ctx.get("adaptive", {}), "replan_on_failure": True}
    if memory is not None:
        ctx["memory"] = memory
    workflow.context = ctx
    session.add(PlanVersion(workflow_id=workflow.id, version=1, status="applied", trigger="initial",
                            reason="initial plan", patch=plan, created_by=created_by))


async def next_plan_version(session: AsyncSession, workflow_id: uuid.UUID) -> int:
    return int(await session.scalar(select(func.coalesce(func.max(PlanVersion.version), 0))
                                    .where(PlanVersion.workflow_id == workflow_id)) or 0) + 1


async def list_plan_versions(session: AsyncSession, workflow_id: uuid.UUID) -> list[PlanVersion]:
    return list(await session.scalars(select(PlanVersion).where(PlanVersion.workflow_id == workflow_id)
                                      .order_by(PlanVersion.version)))


# ------------------------------------------------------------------------------------------- loops

def loop_key(base: str, iteration: int) -> str:
    return base if iteration == 1 else f"{base}__v{iteration}"


def iteration_tasks(tasks: Sequence[Task], loop_name: str, iteration: int) -> dict[str, Task]:
    """Body tasks of one iteration, keyed by their BASE key."""
    return {(t.base_key or t.key): t for t in tasks if t.loop_name == loop_name and (t.iteration or 1) == iteration}


def iteration_outputs(tasks: Sequence[Task], loop_name: str, iteration: int) -> dict[str, dict[str, Any]]:
    return {base: {"output": t.output, "status": str(t.status)}
            for base, t in iteration_tasks(tasks, loop_name, iteration).items()}


async def expand_loop(session: AsyncSession, wf: Workflow, tasks: Sequence[Task], loop_name: str,
                      iteration: int) -> list[Task]:
    """Insert iteration `iteration` of a loop (idempotent: existing rows are returned). No commit.

    First body task depends on its previous iteration + the previous feedback source + its non-body dependencies;
    later body tasks on the new previous body task (+ their non-body dependencies). The feedback target gets
    `revision_feedback` = the reviewer's issues/suggestions/summary/score + the previous draft. Dependents of the
    previous iteration's last task are rewired to the new last task.
    """
    from app.core.events import EventType, record_event

    state = (wf.context or {}).get("loops", {}).get(loop_name)
    if state is None:
        raise ValueError(f"unknown loop {loop_name!r}")
    spec = LoopSpec.model_validate(state["spec"])
    existing = iteration_tasks(tasks, loop_name, iteration)
    if len(existing) == len(spec.body):
        return [existing[b] for b in spec.body]
    prev = iteration_tasks(tasks, loop_name, iteration - 1)
    feedback_src, feedback_to = prev[spec.feedback.from_], spec.feedback.to
    body_ids = {t.id for t in tasks if t.loop_name == loop_name}
    new: dict[str, Task] = {}
    base_time = utcnow_()
    for index, base in enumerate(spec.body):
        template = prev[base]
        deps = [d for d in template.dependencies if d.id not in body_ids]
        if index == 0:
            deps = [*deps, template, feedback_src]
        else:
            deps = [*deps, new[spec.body[index - 1]]]
        task_input = dict(template.input or {})
        task_input.pop("revision_feedback", None)
        if base == feedback_to:
            out = feedback_src.output or {}
            task_input["revision_feedback"] = {
                "iteration": iteration, "score": out.get("score"), "approved": out.get("approved"),
                "issues": out.get("issues", []), "suggestions": out.get("suggestions", []),
                "summary": out.get("summary"), "previous_draft": prev[feedback_to].output,
            }
        t = Task(id=uuid.uuid4(), workflow_id=wf.id, key=loop_key(base, iteration),
                 name=f"{template.name.split(' (v')[0]} (v{iteration})", description=template.description,
                 agent_type=template.agent_type, input=task_input, requires_approval=template.requires_approval,
                 max_attempts=template.max_attempts, loop_name=loop_name, iteration=iteration, base_key=base,
                 join=template.join, run_if=None, replan_if=template.replan_if,
                 created_at=base_time, updated_at=base_time)
        t.dependencies = deps
        new[base] = t
        session.add(t)
    await session.flush()
    await rewire_dependents(session, tasks, prev[spec.body[-1]], new[spec.body[-1]], exclude=body_ids | {
        t.id for t in new.values()})
    state = {**state, "iteration": iteration}
    wf.context = {**wf.context, "loops": {**wf.context["loops"], loop_name: state}}
    for t in new.values():
        record_event(session, workflow_id=wf.id, task_id=t.id, event_type=EventType.TASK_CREATED,
                     payload={"key": t.key, "agent_type": t.agent_type, "depends_on": [d.key for d in t.dependencies],
                              "loop": loop_name, "iteration": iteration})
    record_event(session, workflow_id=wf.id, event_type=EventType.LOOP_ITERATION_STARTED,
                 payload={"loop": loop_name, "iteration": iteration, "tasks": [t.key for t in new.values()]})
    logger.info("loop.iteration_started workflow=%s loop=%s iteration=%d", wf.id, loop_name, iteration)
    return [new[b] for b in spec.body]


async def rewire_dependents(session: AsyncSession, tasks: Sequence[Task], old: Task, new: Task,
                            exclude: set[uuid.UUID] | None = None) -> list[str]:
    """Every task (outside `exclude`) that depends on `old` depends on `new` instead. Returns their keys."""
    moved = []
    for t in tasks:
        if exclude and t.id in exclude or t.id == new.id:
            continue
        if any(d.id == old.id for d in t.dependencies):
            t.dependencies = [new if d.id == old.id else d for d in t.dependencies]
            moved.append(t.key)
    if moved:
        await session.flush()
    return moved


def utcnow_():  # noqa: ANN201
    from app.db.base import utcnow

    return utcnow()
