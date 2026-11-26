"""Planning and graph schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.planner import Plan
from app.core.enums import TaskStatus


class PlanConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int | None = Field(default=None, ge=1, description="Capped at MAX_PLAN_TASKS.")
    notes: str | None = Field(default=None, max_length=4000, description="Extra guidance for the planner.")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraints: PlanConstraints | None = None


class PlanPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: PlanConstraints | None = None


class PlanPreviewResponse(BaseModel):
    plan: Plan
    execution_levels: list[list[str]]
    mermaid: str
    repair_attempts: int
    forced_approvals: list[str] = Field(description="Task keys whose approval was forced by policy.")


class GraphNode(BaseModel):
    key: str
    name: str
    agent_type: str
    status: TaskStatus
    requires_approval: bool
    # Part 10: conditions, joins, why it was skipped, loop iteration
    run_if: str | None = None
    join: str = "all_succeeded"
    skip_reason: str | None = None
    loop_name: str | None = None
    iteration: int = 1
    base_key: str | None = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="Dependency (runs first).")
    to: str = Field(description="Task that needs the dependency's output.")


class WorkflowGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    execution_levels: list[list[str]]
    mermaid: str
