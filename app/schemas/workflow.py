"""Workflow schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import WorkflowStatus
from app.core.state_machine import allowed_controls
from app.db.base import utcnow
from app.models.workflow import Workflow
from app.schemas.plan import PlanConstraints
from app.schemas.task import TaskCreate, TaskRead


class Budget(BaseModel):
    """Per-workflow limits (Part 8). Omitted = config default; explicit null = unlimited."""

    model_config = ConfigDict(extra="forbid")
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    max_llm_calls: int | None = Field(default=None, ge=1)
    max_duration_seconds: float | None = Field(default=None, gt=0)

    def stored(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class BudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(default="admin", min_length=1, max_length=255)
    budget: Budget


class WorkflowCreate(BaseModel):
    """Request body for POST /workflows.

    Structural field checks happen here; graph checks (unique keys, known dependencies,
    no self-dependency) live in the service so the Planner (Part 3) reuses them.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    tasks: list[TaskCreate] = Field(
        default_factory=list,
        max_length=100,
        description="Optional. Leave empty and set auto_plan, or call POST /workflows/{id}/plan.",
    )
    auto_plan: bool = Field(
        default=False, description="If true and no tasks are given, plan in the same request."
    )
    constraints: PlanConstraints | None = Field(
        default=None, description="Planner constraints, used with auto_plan or run."
    )
    run: bool = Field(
        default=False,
        description="If true, return 202 at once; the engine plans (if no tasks) and runs it.",
    )
    budget: Budget | None = Field(default=None, description="Cost/token/call/duration limits (Part 8).")


class WorkflowRead(BaseModel):
    """Workflow summary (used in list responses)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    goal: str
    status: WorkflowStatus
    context: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    run_requested: bool
    budget: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowProgress(BaseModel):
    total: int = 0
    pending: int = 0
    ready: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    retrying: int = 0
    awaiting_approval: int = 0
    skipped: int = 0
    cancelled: int = 0


class PendingApprovals(BaseModel):
    count: int
    ids: list[uuid.UUID]


class Controls(BaseModel):
    """Control actions allowed right now (derived from the state machine)."""

    can_pause: bool
    can_resume: bool
    can_cancel: bool
    can_retry: bool


class WorkflowDetail(WorkflowRead):
    """Workflow with its tasks, their dependencies, progress counts and duration."""

    progress: WorkflowProgress
    effective_budget: dict[str, Any] = Field(default_factory=dict, description="Budget with config defaults.")
    usage: dict[str, Any] | None = Field(default=None, description="LLM calls, tokens, cost, duration so far.")
    remaining: dict[str, Any] | None = Field(default=None, description="Left before each limit (null = unlimited).")
    pending_approvals: PendingApprovals
    controls: Controls
    duration_seconds: float | None = Field(description="Since started_at; until completed_at if finished.")
    tasks: list[TaskRead]

    @classmethod
    def from_model(cls, workflow: Workflow) -> WorkflowDetail:
        """Build from an ORM Workflow with `tasks`, `tasks.dependencies` and `approvals` loaded."""
        summary = WorkflowRead.model_validate(workflow)
        counts = Counter(str(t.status).lower() for t in workflow.tasks)
        pending = [a for a in workflow.approvals if a.status == "pending"]
        duration = None
        if workflow.started_at:
            end = workflow.completed_at or utcnow()
            duration = round((end - workflow.started_at).total_seconds(), 2)
        return cls(
            **summary.model_dump(),
            progress=WorkflowProgress(total=len(workflow.tasks), **counts),
            duration_seconds=duration,
            pending_approvals=PendingApprovals(count=len(pending), ids=[a.id for a in pending]),
            controls=Controls(**allowed_controls(workflow.status)),
            effective_budget=_effective_budget(workflow.budget),
            tasks=[TaskRead.from_model(t) for t in workflow.tasks],
        )


class WorkflowList(BaseModel):
    items: list[WorkflowRead]
    total: int
    limit: int
    offset: int


def _effective_budget(budget: dict[str, Any] | None) -> dict[str, Any]:
    from app.config import get_settings
    from app.services.budget_service import effective_budget

    return effective_budget(budget, get_settings())
