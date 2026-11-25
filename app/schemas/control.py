"""Request/response models for human controls and approvals (Part 6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Actor = Field(default="admin", min_length=1, max_length=255, description="Who did it (audit trail).")


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Actor
    reason: str | None = Field(default=None, max_length=2000)
    mode: Literal["graceful", "immediate"] = Field(
        default="graceful",
        description="graceful: running tasks finish and save. immediate: they are interrupted and "
                    "re-run after resume (no retry is used up).",
    )


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Actor


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Actor
    reason: str | None = Field(default=None, max_length=2000)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=2000)
    input_overrides: dict[str, Any] | None = Field(
        default=None, description="Shallow-merged into the task input before it runs.")


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=2000)
    on_reject: Literal["fail_workflow", "skip_task"] = "fail_workflow"


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    task_id: uuid.UUID
    status: str
    requested_at: datetime
    expires_at: datetime | None
    decided_at: datetime | None
    decided_by: str | None
    comment: str | None
    request_payload: dict[str, Any]
    input_overrides: dict[str, Any] | None
    on_reject: str | None
