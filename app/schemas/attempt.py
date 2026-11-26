"""Attempt history and chaos schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_number: int
    worker_id: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str  # running | succeeded | failed | worker_lost
    error_type: str | None
    error: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None


# crash_after_tool:<tool> (Part 9): hard-exit right after that tool call succeeded and was journaled.
ChaosMode = Literal["crash_process", "raise_transient", "raise_overloaded", "raise_permanent", "hang",
                    "crash_after_tool:send_email", "crash_after_tool:webhook_post", "crash_after_tool:file_write"]


class ChaosCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: uuid.UUID
    task_key: str = Field(min_length=1, max_length=64)
    mode: ChaosMode
    times: int = Field(default=1, ge=1, le=100)


class ChaosRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    task_key: str
    mode: str
    remaining: int
    created_at: datetime
