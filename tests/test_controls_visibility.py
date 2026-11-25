from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import WorkflowStatus
from app.models.workflow import Workflow
from tests.engine_helpers import detail, task

#                    pause  resume cancel retry
EXPECTED = {
    "PENDING":           (False, False, True, False),
    "PLANNING":          (False, False, True, False),
    "RUNNING":           (True, False, True, False),
    "PAUSED":            (False, True, True, False),
    "AWAITING_APPROVAL": (True, False, True, False),
    "COMPLETED":         (False, False, False, False),
    "FAILED":            (False, False, True, True),
    "CANCELLED":         (False, False, False, False),
}


@pytest.mark.parametrize("status", list(EXPECTED))
async def test_controls_per_state(client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
                                  status: str) -> None:
    assert set(EXPECTED) == {s.value for s in WorkflowStatus}
    wf_id = (await client.post("/workflows", json={"name": "x", "goal": "g", "tasks": [task("a")]})).json()["id"]
    async with session_factory() as s:
        await s.execute(update(Workflow).where(Workflow.id == uuid.UUID(wf_id)).values(status=status))
        await s.commit()
    wf = await detail(client, wf_id)
    c = wf["controls"]
    assert (c["can_pause"], c["can_resume"], c["can_cancel"], c["can_retry"]) == EXPECTED[status]
    assert wf["pending_approvals"] == {"count": 0, "ids": []}
