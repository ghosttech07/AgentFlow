"""Event schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_id: uuid.UUID
    task_id: uuid.UUID | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
