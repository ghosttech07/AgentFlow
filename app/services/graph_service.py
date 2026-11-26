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
