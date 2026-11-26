"""Durable tool journal: stable keys, reuse rules, unknown outcomes, redaction, executed-call counting."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.models import Task, ToolCall, Workflow
from app.tools import journal


async def _task(sf: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    async with sf() as s:
        wf = Workflow(id=uuid.uuid4(), name="w", goal="g", status="RUNNING")
        s.add(wf)
        await s.flush()
        t = Task(id=uuid.uuid4(), workflow_id=wf.id, key="t", name="t", agent_type="writer", status="RUNNING")
        s.add(t)
        await s.commit()
        return wf.id, t.id


def test_hash_is_stable_across_key_order() -> None:
    assert journal.args_hash({"a": 1, "b": [1, 2]}) == journal.args_hash({"b": [1, 2], "a": 1})
    wf = uuid.uuid4()
    k1 = journal.idempotency_key(wf, "t", "send_email", journal.args_hash({"to": ["x"]}))
    assert k1 == journal.idempotency_key(wf, "t", "send_email", journal.args_hash({"to": ["x"]}))
    assert k1 != journal.idempotency_key(wf, "t2", "send_email", journal.args_hash({"to": ["x"]}))


def _row(status: str, age_s: float = 0) -> ToolCall:
    now = utcnow()
    return ToolCall(status=status, finished_at=now - timedelta(seconds=age_s), started_at=now)


def test_reuse_decisions() -> None:
    now = utcnow()
    d = journal.reuse_decision
    assert d(None, "external_write", True, now, 3600) == "execute"
    assert d(_row("succeeded", age_s=99999), "external_write", False, now, 3600) == "reuse"  # writes: any age
    assert d(_row("succeeded", age_s=99999), "internal_write", True, now, 3600) == "reuse"
    assert d(_row("succeeded", age_s=10), "external_read", False, now, 3600) == "reuse"
    assert d(_row("succeeded", age_s=7200), "external_read", False, now, 3600) == "execute"  # stale read
    assert d(_row("succeeded", age_s=1), "none", False, now, 0) == "execute"  # reuse window 0 = never
    assert d(_row("running"), "external_write", True, now, 3600) == "unknown_reexecute"
    assert d(_row("unknown_outcome"), "external_write", False, now, 3600) == "unknown_needs_approval"
    assert d(_row("running"), "external_read", False, now, 3600) == "execute"
    assert d(_row("failed"), "external_write", False, now, 3600) == "execute"


async def test_record_lifecycle_and_find_previous(session_factory) -> None:  # noqa: ANN001
    wf_id, task_id = await _task(session_factory)
    key = journal.idempotency_key(wf_id, "t", "calculator", journal.args_hash({"expression": "1+1"}))
    call_id = await journal.record_start(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=1,
                                         step_index=0, tool="calculator", arguments={"expression": "1+1"},
                                         idempotency_key=key, side_effect="none")
    async with session_factory() as s:
        assert (await journal.find_previous(s, key)).status == "running"
    await journal.record_finish(session_factory, call_id, status="succeeded", result={"value": 2}, duration_ms=3)
    async with session_factory() as s:
        prev = await journal.find_previous(s, key)
    assert (prev.status, prev.result, prev.duration_ms) == ("succeeded", {"value": 2}, 3) and prev.finished_at


async def test_mark_unknown_and_transcript_order(session_factory) -> None:  # noqa: ANN001
    wf_id, task_id = await _task(session_factory)
    a = await journal.record_step(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=1,
                                  step_index=1, tool="web_fetch", status="rejected_invalid", side_effect="external_read",
                                  error="bad args")
    b = await journal.record_start(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=1,
                                   step_index=0, tool="send_email", arguments={}, idempotency_key="k",
                                   side_effect="external_write")
    await journal.mark_unknown(session_factory, b)
    rows = await journal.transcript(session_factory, task_id, [1])
    assert [(r.id, r.status) for r in rows] == [(b, "unknown_outcome"), (a, "rejected_invalid")]


async def test_secret_never_in_journal(session_factory) -> None:  # noqa: ANN001
    wf_id, task_id = await _task(session_factory)
    secret = "ghp_NEVER_STORE_ME"
    cid = await journal.record_start(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=1,
                                     step_index=0, tool="http_request",
                                     arguments={"url": "https://api.github.com", "note": f"x {secret}"},
                                     idempotency_key="k", side_effect="external_read", secrets=[secret])
    await journal.record_finish(session_factory, cid, status="failed", error=f"401 with token {secret}",
                                result={"headers": {"Authorization": f"Bearer {secret}"}}, secrets=[secret])
    async with session_factory() as s:
        row = await s.get(ToolCall, cid)
    dumped = repr((row.arguments, row.result, row.error))
    assert secret not in dumped and "***" in dumped


async def test_executed_tool_calls_excludes_reused_and_unexecuted(session_factory) -> None:  # noqa: ANN001
    wf_id, task_id = await _task(session_factory)
    done = await journal.record_start(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=1,
                                      step_index=0, tool="calculator", arguments={}, idempotency_key="k1",
                                      side_effect="none")
    await journal.record_step(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=2, step_index=0,
                              tool="calculator", status="succeeded", side_effect="none", reused_from_id=done)
    await journal.record_step(session_factory, workflow_id=wf_id, task_id=task_id, attempt_number=2, step_index=1,
                              tool="send_email", status="awaiting_approval", side_effect="external_write")
    async with session_factory() as s:
        assert await journal.executed_tool_calls(s, wf_id) == 1
        assert len((await s.scalars(select(ToolCall))).all()) == 3
