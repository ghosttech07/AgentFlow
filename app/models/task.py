"""Task ORM model and the task_dependencies association table (edges of the DAG)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import TaskStatus
from app.db.base import Base, status_column_type, utcnow

if TYPE_CHECKING:
    from app.models.workflow import Workflow

# Edge "task_id depends on depends_on_task_id". Rows are deleted with either task.
task_dependencies = Table(
    "task_dependencies",
    Base.metadata,
    Column(
        "task_id",
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "depends_on_task_id",
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,  # fast "who depends on X" lookups (dependents)
    ),
    CheckConstraint("task_id <> depends_on_task_id", name="no_self_dependency"),
)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workflow_id", "key"),
        Index("ix_tasks_workflow_id_status", "workflow_id", "status"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE")
    )
    # Short identifier unique within a workflow, used to express dependencies in requests.
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Free-form for now; validated against the agent registry from Part 2.
    agent_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[TaskStatus] = mapped_column(
        status_column_type(TaskStatus, "task_status"),
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )

    input: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    output: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    # transient | permanent | output | timeout | unknown (from the Part 2 LLM error classes)
    error_type: Mapped[str | None] = mapped_column(String(32))
    # Last run: model, input_tokens, output_tokens, latency_ms, sources
    metrics: Mapped[dict[str, Any] | None] = mapped_column()

    requires_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # Part 10: loops (unrolled as <base>__v<n>), conditional branches, re-plan triggers.
    loop_name: Mapped[str | None] = mapped_column(String(64))
    iteration: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    base_key: Mapped[str | None] = mapped_column(String(64))
    run_if: Mapped[str | None] = mapped_column(Text)
    join: Mapped[str] = mapped_column(String(16), default="all_succeeded", server_default="all_succeeded")
    replan_if: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    # Part 9: an attempt read untrusted content (web/API); downstream tasks start tainted.
    tainted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))

    # Reliability fields - populated from Part 5 (idempotency, leases, heartbeats).
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    last_heartbeat_at: Mapped[datetime | None] = mapped_column()
    # When a RETRYING task may run again (Part 5 backoff).
    next_attempt_at: Mapped[datetime | None] = mapped_column()

    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()

    workflow: Mapped[Workflow] = relationship(back_populates="tasks")

    # Tasks this task depends on (must finish before this one can run).
    dependencies: Mapped[list[Task]] = relationship(
        secondary=task_dependencies,
        primaryjoin=lambda: Task.id == task_dependencies.c.task_id,
        secondaryjoin=lambda: Task.id == task_dependencies.c.depends_on_task_id,
        back_populates="dependents",
    )
    # Tasks that depend on this task.
    dependents: Mapped[list[Task]] = relationship(
        secondary=task_dependencies,
        primaryjoin=lambda: Task.id == task_dependencies.c.depends_on_task_id,
        secondaryjoin=lambda: Task.id == task_dependencies.c.task_id,
        back_populates="dependencies",
    )

    def __repr__(self) -> str:
        return f"<Task id={self.id} key={self.key} status={self.status}>"
