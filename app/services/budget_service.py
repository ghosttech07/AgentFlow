"""Workflow budgets (Part 8). Usage = SQL sums over llm_calls (one source of truth, so totals can
never drift from the calls actually made, planner and failed calls included). A reached limit PAUSES
the workflow (graceful, like the Part 6 pause) for a human to decide; it never fails it.

Limits (all optional, None = unlimited): max_cost_usd, max_tokens (input + output), max_llm_calls,
max_duration_seconds (since started_at). Defaults: DEFAULT_MAX_* settings (DEFAULT_MAX_LLM_CALLS=50).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.enums import WorkflowStatus
from app.core.errors import NotFoundError
from app.core.events import EventType, record_event
from app.core.state_machine import transition_workflow
from app.db.base import utcnow
from app.core.enums import TaskStatus
from app.models.task import Task
from app.models.usage import LLMCall
from app.models.workflow import Workflow

UNFINISHED = (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.RETRYING,
              TaskStatus.AWAITING_APPROVAL)

logger = logging.getLogger(__name__)

LIMITS = ("max_llm_calls", "max_tokens", "max_cost_usd", "max_duration_seconds")  # check order
PAUSABLE = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL)


@dataclass
class Usage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0  # calls whose price is unknown (cost null): max_cost_usd cannot see them
    duration_seconds: float | None = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "tokens": self.tokens}


def effective_budget(budget: dict[str, Any] | None, settings: Settings) -> dict[str, Any]:
    eff: dict[str, Any] = {"max_cost_usd": settings.default_max_cost_usd, "max_tokens": settings.default_max_tokens,
                           "max_llm_calls": settings.default_max_llm_calls,
                           "max_duration_seconds": settings.default_max_duration_seconds}
    eff.update(budget or {})  # an explicit null in the workflow budget means unlimited
    return eff


async def get_usage(session: AsyncSession, wf: Workflow, now: datetime | None = None) -> Usage:
    row = (await session.execute(
        select(func.count(), func.coalesce(func.sum(LLMCall.input_tokens), 0),
               func.coalesce(func.sum(LLMCall.output_tokens), 0), func.coalesce(func.sum(LLMCall.cost_usd), 0),
               func.count().filter(LLMCall.cost_usd.is_(None)))
        .where(LLMCall.workflow_id == wf.id)
    )).one()
    duration = round(((now or utcnow()) - wf.started_at).total_seconds(), 2) if wf.started_at else None
    return Usage(llm_calls=row[0], input_tokens=int(row[1]), output_tokens=int(row[2]), cost_usd=float(row[3]),
                 unpriced_calls=row[4], duration_seconds=duration)


def exceeded(eff: dict[str, Any], usage: Usage) -> str | None:
    """The first limit reached (>=), or None."""
    used = {"max_llm_calls": usage.llm_calls, "max_tokens": usage.tokens, "max_cost_usd": usage.cost_usd,
            "max_duration_seconds": usage.duration_seconds}
    for limit in LIMITS:
        if eff.get(limit) is not None and used[limit] is not None and used[limit] >= eff[limit]:
            return limit
    return None


def remaining(eff: dict[str, Any], usage: Usage) -> dict[str, Any]:
    pairs = {"llm_calls": ("max_llm_calls", usage.llm_calls), "tokens": ("max_tokens", usage.tokens),
             "cost_usd": ("max_cost_usd", usage.cost_usd),
             "duration_seconds": ("max_duration_seconds", usage.duration_seconds or 0.0)}
    return {name: (None if eff.get(limit) is None else max(0, round(eff[limit] - used, 6)))
            for name, (limit, used) in pairs.items()}


async def typical_output_tokens(session: AsyncSession, role: str | None) -> int | None:
    """Average output tokens of this role's last 20 successful calls (pre-call cost estimate)."""
    recent = (select(LLMCall.output_tokens).where(LLMCall.role == role, LLMCall.outcome == "succeeded")
              .order_by(LLMCall.id.desc()).limit(20).subquery())
    avg = await session.scalar(select(func.avg(recent.c.output_tokens)))
    return int(avg) if avg is not None else None


async def pause_locked(session: AsyncSession, wf: Workflow, limit: str, usage: Usage, eff: dict[str, Any],
                       estimated: bool = False) -> bool:
    """Pause a locked RUNNING/AWAITING_APPROVAL workflow for its budget (graceful). No commit."""
    if wf.status not in PAUSABLE:
        return False  # e.g. PLANNING: the scheduler pauses it as soon as it runs
    await transition_workflow(session, wf, WorkflowStatus.PAUSED, event_type=EventType.WORKFLOW_PAUSED,
                              reason=f"budget_exceeded: {limit}", extra={"mode": "graceful", "actor": "budget"})
    record_event(session, workflow_id=wf.id, event_type=EventType.WORKFLOW_BUDGET_EXCEEDED,
                 payload={"limit": limit, "estimated": estimated, "usage": usage.as_dict(),
                          "budget": {k: eff.get(k) for k in LIMITS}})
    logger.warning("workflow.budget_exceeded id=%s limit=%s estimated=%s usage=%s", wf.id, limit, estimated,
                   usage.as_dict())
    return True


async def pause_for_budget(sf: async_sessionmaker[AsyncSession], workflow_id: uuid.UUID, limit: str,
                           settings: Settings, estimated: bool = False,
                           current_task_id: uuid.UUID | None = None) -> bool:
    async with sf() as session:
        wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
        if wf is None:
            return False
        if current_task_id is not None and not estimated:
            # After a call: if no OTHER task still has work to do, let this one finish the workflow.
            others = await session.scalar(select(func.count()).select_from(Task).where(
                Task.workflow_id == workflow_id, Task.id != current_task_id, Task.status.in_(UNFINISHED)))
            if not others:
                await session.rollback()
                return False
        eff = effective_budget(wf.budget, settings)
        paused = await pause_locked(session, wf, limit, await get_usage(session, wf), eff, estimated)
        await session.commit()
        return paused


async def update_budget(session: AsyncSession, workflow_id: uuid.UUID, *, actor: str,
                        budget: dict[str, Any]) -> None:
    """Raise or adjust limits (merged onto the current budget). The human then resumes as usual."""
    wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if wf is None:
        raise NotFoundError(f"Workflow {workflow_id} not found")
    before = dict(wf.budget or {})
    wf.budget = {**before, **budget}
    record_event(session, workflow_id=wf.id, event_type=EventType.WORKFLOW_BUDGET_UPDATED,
                 payload={"actor": actor, "from": before, "to": wf.budget})
    await session.commit()
    logger.info("workflow.budget_updated id=%s actor=%s budget=%s", wf.id, actor, wf.budget)
