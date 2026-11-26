"""Part 10 schema + states: loop/branch columns, task-less approvals, plan_versions, memories (full-text), new statuses."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import InvalidTransitionError
from app.core.state_machine import transition_task, transition_workflow
from app.models import Approval, Memory, PlanVersion, Task, Workflow


async def _wf(s, status: str = "RUNNING") -> Workflow:  # noqa: ANN001
    wf = Workflow(id=uuid.uuid4(), name="w", goal="g", status=status)
    s.add(wf)
    await s.flush()
    return wf


async def test_task_loop_and_branch_columns_default(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        wf = await _wf(s)
        t = Task(id=uuid.uuid4(), workflow_id=wf.id, key="w", name="w", agent_type="writer")
        s.add(t)
        await s.commit()
        row = await s.get(Task, t.id)
    assert (row.iteration, row.join, row.loop_name, row.run_if, row.skip_reason) == (1, "all_succeeded", None, None, None)


async def test_approval_without_a_task(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        wf = await _wf(s)
        s.add(Approval(workflow_id=wf.id, task_id=None, kind="plan_patch", request_payload={"diff": []}))
        await s.commit()
        assert (await s.scalar(select(Approval))).task_id is None


async def test_plan_versions_are_unique_per_workflow(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        wf = await _wf(s)
        s.add(PlanVersion(workflow_id=wf.id, version=1, trigger="initial", patch={}, created_by="planner"))
        await s.commit()
        s.add(PlanVersion(workflow_id=wf.id, version=1, trigger="human", patch={}, created_by="dana"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_memory_full_text_search(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        s.add_all([Memory(kind="fact", content="India sold 2.3 lakh electric vehicles in October 2026"),
                   Memory(kind="fact", content="Solar capacity in Kenya grew in 2025")])
        await s.commit()
        hits = (await s.scalars(select(Memory.content).where(
            Memory.search_vector.op("@@")(func.websearch_to_tsquery("english", "electric vehicles India"))))).all()
        assert hits == ["India sold 2.3 lakh electric vehicles in October 2026"]
        assert await s.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_memories_search_vector'")) == 1


async def test_new_statuses_and_transitions(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        wf = await _wf(s)
        await transition_workflow(s, wf, WorkflowStatus.REPLANNING, reason="task failed")
        await transition_workflow(s, wf, WorkflowStatus.RUNNING, reason="patch applied")
        t = Task(id=uuid.uuid4(), workflow_id=wf.id, key="r", name="r", agent_type="researcher", status=TaskStatus.FAILED)
        s.add(t)
        await s.flush()
        await transition_task(s, t, TaskStatus.SUPERSEDED, reason="replaced by r_alt")
        with pytest.raises(InvalidTransitionError):
            await transition_task(s, t, TaskStatus.PENDING)  # terminal
        with pytest.raises(InvalidTransitionError):
            await transition_workflow(s, wf, WorkflowStatus.PLANNING)
        await s.commit()
    assert t.status == TaskStatus.SUPERSEDED and t.completed_at is not None
