"""Migration 0006 schema: tool_calls journal, approvals.kind/tool_call_id, attempts.loop_state, tasks.tainted."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import TaskStatus
from app.core.state_machine import transition_task
from app.models import Approval, Task, TaskAttempt, ToolCall, Workflow, WorkflowEvent


async def _wf_task(s: AsyncSession, status: TaskStatus = TaskStatus.RUNNING) -> tuple[Workflow, Task]:
    wf = Workflow(id=uuid.uuid4(), name="w", goal="g", status="RUNNING")
    s.add(wf)
    await s.flush()
    task = Task(id=uuid.uuid4(), workflow_id=wf.id, key="t", name="t", agent_type="writer", status=status)
    s.add(task)
    await s.flush()
    return wf, task


async def test_running_task_can_await_approval(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        _, task = await _wf_task(s)
        await transition_task(s, task, TaskStatus.AWAITING_APPROVAL, reason="tool_call")
        await s.commit()
        events = (await s.scalars(select(WorkflowEvent).where(WorkflowEvent.task_id == task.id))).all()
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert [e.payload["to"] for e in events] == ["AWAITING_APPROVAL"]


async def test_tool_call_row_roundtrip(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        wf, task = await _wf_task(s)
        s.add(ToolCall(workflow_id=wf.id, task_id=task.id, attempt_number=1, step_index=0, tool="calculator",
                       arguments={"expression": "1+1"}, args_hash="h", idempotency_key="k", side_effect="none"))
        s.add(TaskAttempt(task_id=task.id, workflow_id=wf.id, attempt_number=1, worker_id="w",
                          loop_state={"step_index": 1}))
        await s.commit()
        row = await s.scalar(select(ToolCall))
        attempt = await s.scalar(select(TaskAttempt))
        task_row = await s.get(Task, task.id)
    assert (row.status, row.tainted_context, row.reused_from_id, row.approval_id) == ("proposed", False, None, None)
    assert attempt.loop_state == {"step_index": 1}
    assert task_row.tainted is False


async def test_approval_kind_defaults_to_task(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as s:
        wf, task = await _wf_task(s, TaskStatus.AWAITING_APPROVAL)
        s.add(Approval(workflow_id=wf.id, task_id=task.id))
        await s.commit()
        approval = await s.scalar(select(Approval))
    assert (approval.kind, approval.tool_call_id) == ("task", None)
