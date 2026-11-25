"""Startup recovery: run once before the engine's first tick.

- RUNNING tasks: with ENGINE_SINGLE_NODE they belong to a dead previous process, so reclaim them
  now (worker_lost -> retry policy). With multiple engines they are left to the reaper, which
  only reclaims them once their lease has expired (a live engine keeps heartbeating).
- PLANNING workflows: planning died mid-call. Tasks are inserted in one transaction, so there is
  never a partial plan: move them back to PENDING with run_requested=true to be re-planned.
- RUNNING workflows: nothing to do; the scheduler continues them from DB state.
- SUCCEEDED tasks are never touched and their agents are never called again.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.events import EventType, record_event
from app.core.state_machine import transition_workflow
from app.engine.retry import reclaim_lost_task
from app.models.task import Task
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)


async def recover_on_startup(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, worker_id: str
) -> dict[str, Any]:
    reclaimed: dict[Any, list[str]] = defaultdict(list)
    replanned: list[Any] = []
    async with session_factory() as session:
        if settings.engine_single_node:
            tasks = (
                await session.scalars(
                    select(Task).where(Task.status == TaskStatus.RUNNING).with_for_update(skip_locked=True)
                )
            ).all()
            for t in tasks:
                await reclaim_lost_task(session, t, settings, reason="process restarted")
                reclaimed[t.workflow_id].append(t.key)

        stuck = (
            await session.scalars(
                select(Workflow).where(Workflow.status == WorkflowStatus.PLANNING)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for wf in stuck:
            wf.run_requested = True
            await transition_workflow(
                session, wf, WorkflowStatus.PENDING, event_type=EventType.WORKFLOW_PLANNING_RECOVERED,
                reason="planning interrupted by a restart; will be re-planned",
            )
            replanned.append(wf.id)

        running = await session.scalar(
            select(func.count()).select_from(Workflow).where(Workflow.status == WorkflowStatus.RUNNING)
        )
        counts = {
            "reclaimed_tasks": sum(len(v) for v in reclaimed.values()),
            "replanned_workflows": len(replanned),
            "running_workflows": running or 0,
            "single_node": settings.engine_single_node,
            "worker_id": worker_id,
        }
        # engine.recovered is recorded on every workflow the recovery touched.
        for workflow_id in {*reclaimed, *replanned}:
            record_event(
                session, workflow_id=workflow_id, event_type=EventType.ENGINE_RECOVERED,
                payload={**counts, "reclaimed_task_keys": reclaimed.get(workflow_id, []),
                         "replanned": workflow_id in replanned},
            )
        await session.commit()
    logger.warning("engine.recovered %s", counts) if (reclaimed or replanned) else \
        logger.info("engine.recovered nothing to recover %s", counts)
    return counts
