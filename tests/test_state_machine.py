from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import InvalidTransitionError
from app.core.state_machine import (
    TASK_TERMINAL_STATES,
    TASK_TRANSITIONS,
    WORKFLOW_TERMINAL_STATES,
    WORKFLOW_TRANSITIONS,
    can_transition_task,
    can_transition_workflow,
    transition_task,
    transition_workflow,
)
from app.models import Task, Workflow, WorkflowEvent

W = WorkflowStatus
T = TaskStatus


async def make_workflow(session: AsyncSession, status: WorkflowStatus = W.PENDING) -> Workflow:
    wf = Workflow(name="sm", goal="state machine test", status=status)
    session.add(wf)
    await session.commit()
    return wf


async def make_task(session: AsyncSession, wf: Workflow, status: TaskStatus = T.PENDING) -> Task:
    task = Task(workflow_id=wf.id, key="t1", name="t1", agent_type="researcher", status=status)
    session.add(task)
    await session.commit()
    return task


async def events_for(session: AsyncSession, wf: Workflow) -> list[WorkflowEvent]:
    rows = await session.execute(
        select(WorkflowEvent).where(WorkflowEvent.workflow_id == wf.id).order_by(WorkflowEvent.id)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------- pure rules


def test_every_status_has_a_transition_entry() -> None:
    assert set(WORKFLOW_TRANSITIONS) == set(WorkflowStatus)
    assert set(TASK_TRANSITIONS) == set(TaskStatus)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (W.PENDING, W.PLANNING),
        (W.PENDING, W.RUNNING),
        (W.PLANNING, W.RUNNING),
        (W.RUNNING, W.PAUSED),
        (W.PAUSED, W.RUNNING),
        (W.RUNNING, W.AWAITING_APPROVAL),
        (W.AWAITING_APPROVAL, W.RUNNING),
        (W.RUNNING, W.COMPLETED),
        (W.RUNNING, W.FAILED),
        (W.FAILED, W.RUNNING),  # resume exception
        (W.FAILED, W.PLANNING),  # re-plan after failed planning
        (W.PLANNING, W.PENDING),  # plan stored, ready to run
        (W.PLANNING, W.FAILED),
        (W.PAUSED, W.CANCELLED),
        (W.PAUSED, W.AWAITING_APPROVAL),  # resume while only approvals block it (P6)
        (W.AWAITING_APPROVAL, W.PAUSED),
        (W.FAILED, W.CANCELLED),  # close a failed workflow (P6)
    ],
)
def test_allowed_workflow_transitions(src: WorkflowStatus, dst: WorkflowStatus) -> None:
    assert can_transition_workflow(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (W.PENDING, W.COMPLETED),
        (W.PAUSED, W.COMPLETED),
        (W.RUNNING, W.RUNNING),
        (W.RUNNING, W.PENDING),
        (W.FAILED, W.COMPLETED),
        (W.FAILED, W.PENDING),
        (W.PLANNING, W.PAUSED),  # pause is only for RUNNING / AWAITING_APPROVAL
        (W.CANCELLED, W.RUNNING),
    ],
)
def test_disallowed_workflow_transitions(src: WorkflowStatus, dst: WorkflowStatus) -> None:
    assert not can_transition_workflow(src, dst)


def test_workflow_terminal_states_have_no_exits() -> None:
    assert WORKFLOW_TERMINAL_STATES == {W.COMPLETED, W.CANCELLED}
    for terminal in WORKFLOW_TERMINAL_STATES:
        assert all(not can_transition_workflow(terminal, s) for s in WorkflowStatus)
    # FAILED only allows resume (Part 5), re-planning (Part 3) or closing it (Part 6).
    assert WORKFLOW_TRANSITIONS[W.FAILED] == {W.RUNNING, W.PLANNING, W.CANCELLED}


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (T.PENDING, T.READY),
        (T.READY, T.RUNNING),
        (T.READY, T.AWAITING_APPROVAL),
        (T.AWAITING_APPROVAL, T.READY),
        (T.RUNNING, T.SUCCEEDED),
        (T.RUNNING, T.FAILED),
        (T.RUNNING, T.RETRYING),
        (T.RETRYING, T.RUNNING),
        (T.FAILED, T.RETRYING),  # retry exception
        (T.FAILED, T.PENDING),  # recovery exception
        (T.PENDING, T.SKIPPED),
        (T.RUNNING, T.READY),  # interrupted by an immediate pause (P6)
        (T.AWAITING_APPROVAL, T.FAILED),  # rejected / approval expired (P6)
        (T.AWAITING_APPROVAL, T.SKIPPED),
        (T.AWAITING_APPROVAL, T.CANCELLED),
    ],
)
def test_allowed_task_transitions(src: TaskStatus, dst: TaskStatus) -> None:
    assert can_transition_task(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (T.PENDING, T.RUNNING),  # must become READY first
        (T.PENDING, T.SUCCEEDED),
        (T.SUCCEEDED, T.RUNNING),
        (T.SKIPPED, T.READY),
        (T.CANCELLED, T.PENDING),
        (T.FAILED, T.SUCCEEDED),
        (T.FAILED, T.RUNNING),
    ],
)
def test_disallowed_task_transitions(src: TaskStatus, dst: TaskStatus) -> None:
    assert not can_transition_task(src, dst)


def test_task_terminal_states_have_no_exits() -> None:
    assert TASK_TERMINAL_STATES == {T.SUCCEEDED, T.CANCELLED, T.SKIPPED}
    assert TASK_TRANSITIONS[T.FAILED] == {T.RETRYING, T.PENDING}


# ---------------------------------------------------------------- persisted transitions


async def test_workflow_transition_sets_timestamps_and_records_events(
    db_session: AsyncSession,
) -> None:
    wf = await make_workflow(db_session)

    await transition_workflow(db_session, wf, W.RUNNING)
    await db_session.commit()
    first_started = wf.started_at
    assert wf.status is W.RUNNING
    assert first_started is not None and first_started.tzinfo is not None
    assert wf.completed_at is None

    await transition_workflow(db_session, wf, W.PAUSED, reason="user paused")
    await transition_workflow(db_session, wf, W.RUNNING)
    assert wf.started_at == first_started  # only set the first time

    await transition_workflow(db_session, wf, W.COMPLETED)
    await db_session.commit()
    assert wf.completed_at is not None and wf.completed_at >= first_started

    events = await events_for(db_session, wf)
    assert [e.event_type for e in events] == ["workflow.status_changed"] * 4
    assert [(e.payload["from"], e.payload["to"]) for e in events] == [
        ("PENDING", "RUNNING"),
        ("RUNNING", "PAUSED"),
        ("PAUSED", "RUNNING"),
        ("RUNNING", "COMPLETED"),
    ]
    assert events[1].payload["reason"] == "user paused"
    assert all(e.task_id is None for e in events)

    # Persisted, not just in memory.
    db_session.expunge_all()
    reloaded = await db_session.get(Workflow, wf.id)
    assert reloaded is not None and reloaded.status is W.COMPLETED


async def test_invalid_workflow_transition_raises_and_changes_nothing(
    db_session: AsyncSession,
) -> None:
    wf = await make_workflow(db_session)
    await transition_workflow(db_session, wf, W.RUNNING)
    await transition_workflow(db_session, wf, W.COMPLETED)
    await db_session.commit()

    with pytest.raises(InvalidTransitionError) as exc_info:
        await transition_workflow(db_session, wf, W.RUNNING)
    assert exc_info.value.from_status == "COMPLETED"
    assert exc_info.value.to_status == "RUNNING"
    assert wf.status is W.COMPLETED
    assert len(await events_for(db_session, wf)) == 2


async def test_workflow_resume_from_failed_clears_completed_at(db_session: AsyncSession) -> None:
    wf = await make_workflow(db_session, W.RUNNING)
    await transition_workflow(db_session, wf, W.FAILED)
    assert wf.completed_at is not None

    await transition_workflow(db_session, wf, W.RUNNING, reason="resume")
    assert wf.status is W.RUNNING
    assert wf.completed_at is None


async def test_task_transition_sets_timestamps_and_records_events(
    db_session: AsyncSession,
) -> None:
    wf = await make_workflow(db_session)
    task = await make_task(db_session, wf)

    await transition_task(db_session, task, T.READY)
    assert task.started_at is None
    await transition_task(db_session, task, T.RUNNING)
    started = task.started_at
    assert started is not None

    await transition_task(db_session, task, T.FAILED, reason="boom")
    assert task.completed_at is not None
    await transition_task(db_session, task, T.RETRYING)
    assert task.completed_at is None
    await transition_task(db_session, task, T.RUNNING)
    assert task.started_at == started  # first start preserved across retries
    await transition_task(db_session, task, T.SUCCEEDED, extra={"attempt": 2})
    await db_session.commit()
    assert task.completed_at is not None

    events = await events_for(db_session, wf)
    assert all(e.event_type == "task.status_changed" for e in events)
    assert all(e.task_id == task.id for e in events)
    assert all(e.payload["task_key"] == "t1" for e in events)
    assert [(e.payload["from"], e.payload["to"]) for e in events] == [
        ("PENDING", "READY"),
        ("READY", "RUNNING"),
        ("RUNNING", "FAILED"),
        ("FAILED", "RETRYING"),
        ("RETRYING", "RUNNING"),
        ("RUNNING", "SUCCEEDED"),
    ]
    assert events[2].payload["reason"] == "boom"
    assert events[5].payload["attempt"] == 2


async def test_invalid_task_transition_raises(db_session: AsyncSession) -> None:
    wf = await make_workflow(db_session)
    task = await make_task(db_session, wf)

    with pytest.raises(InvalidTransitionError, match="PENDING -> SUCCEEDED"):
        await transition_task(db_session, task, T.SUCCEEDED)
    assert task.status is T.PENDING
    assert await events_for(db_session, wf) == []

    await transition_task(db_session, task, T.SKIPPED)
    with pytest.raises(InvalidTransitionError):
        await transition_task(db_session, task, T.READY)
