"""Task schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.core.enums import TaskStatus
from app.models.task import Task


class TaskCreate(BaseModel):
    """One node of the task graph in a create-workflow request."""

    model_config = ConfigDict(extra="forbid")

    # Format/emptiness rules live in app.core.dag.validate_plan so ALL errors are reported together.
    key: str = Field(
        max_length=64,
        description="Lowercase snake_case, unique within the workflow, e.g. 'research_1'.",
    )
    name: str = Field(max_length=255)
    description: str = ""
    agent_type: str = Field(min_length=1, max_length=64, description="e.g. 'researcher'")
    input: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    max_attempts: int = Field(
        default_factory=lambda: get_settings().default_task_max_attempts, ge=1, le=20
    )
    depends_on: list[str] = Field(
        default_factory=list, description="Keys of tasks in the same request."
    )


class TaskRead(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    key: str
    name: str
    description: str
    agent_type: str
    status: TaskStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    error_type: str | None = Field(description="transient | permanent | output | timeout | unknown")
    metrics: dict[str, Any] | None = Field(description="model, input/output tokens, latency_ms, sources")
    requires_approval: bool
    attempt_count: int
    max_attempts: int
    depends_on: list[str] = Field(description="Keys of the tasks this task depends on.")
    idempotency_key: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_model(cls, task: Task) -> TaskRead:
        """Build from an ORM Task whose `dependencies` relationship is already loaded."""
        return cls(
            id=task.id,
            workflow_id=task.workflow_id,
            key=task.key,
            name=task.name,
            description=task.description,
            agent_type=task.agent_type,
            status=task.status,
            input=task.input,
            output=task.output,
            error=task.error,
            error_type=task.error_type,
            metrics=task.metrics,
            requires_approval=task.requires_approval,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            depends_on=sorted(dep.key for dep in task.dependencies),
            idempotency_key=task.idempotency_key,
            lease_owner=task.lease_owner,
            lease_expires_at=task.lease_expires_at,
            last_heartbeat_at=task.last_heartbeat_at,
            next_attempt_at=task.next_attempt_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            started_at=task.started_at,
            completed_at=task.completed_at,
        )
