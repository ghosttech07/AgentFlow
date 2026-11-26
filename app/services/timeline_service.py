"""Workflow timeline (Part 7): the event log turned into something a human can read.

Built only from persisted rows (workflow, tasks, task_attempts, workflow_events), so it tells the
same story after a crash/restart: offsets are seconds since the workflow was created.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attempt import TaskAttempt
from app.models.event import WorkflowEvent
from app.models.tool_call import ToolCall
from app.models.usage import LLMCall
from app.services.workflow_service import get_workflow


def _offset(at: datetime | None, origin: datetime) -> float | None:
    return None if at is None else round((at - origin).total_seconds(), 2)


def _via(p: dict[str, Any]) -> str:
    return f"{p['provider']}:{p.get('model')}" if p.get("provider") else str(p.get("model"))


def _fallback(p: dict[str, Any]) -> str:
    return f", fallback from {p['fallback_from']}" if p.get("fallback_from") else ""


def describe(event_type: str, p: dict[str, Any]) -> str:
    """One readable line per event. Unknown types fall back to the type + from/to."""
    key = p.get("task_key") or p.get("key")
    who = f" by {p['actor']}" if p.get("actor") else ""
    why = f": {p['reason']}" if p.get("reason") else ""
    texts = {
        "workflow.created": f"Workflow created ({p.get('task_count', 0)} tasks)",
        "workflow.planning_started": "Planning started",
        "workflow.plan_created": f"Plan created: {p.get('task_count')} tasks, "
                                 f"{len(p.get('execution_levels') or [])} levels",
        "workflow.planning_failed": f"Planning failed ({p.get('error_type')})",
        "workflow.planning_recovered": "Planning interrupted by a restart; will re-plan",
        "workflow.started": "Workflow started",
        "workflow.completed": f"Workflow completed in {p.get('duration_seconds')}s",
        "workflow.failed": f"Workflow failed{why}",
        "workflow.awaiting_approval": f"Waiting for approval of {', '.join(p.get('tasks', []))}",
        "workflow.paused": f"Paused ({p.get('mode')}){who}{why}",
        "workflow.resumed": f"Resumed{who} -> {p.get('to')}",
        "workflow.cancelled": f"Cancelled{who}{why} ({p.get('cancelled_tasks', 0)} tasks)",
        "workflow.retried": f"Manual retry of {p.get('reset_tasks') or 'planning'}",
        "engine.recovered": f"Engine restarted ({p.get('worker_id')}); reclaimed "
                            f"{p.get('reclaimed_task_keys') or 'nothing'}",
        "task.created": f"Task {key} created ({p.get('agent_type')})",
        "task.ready": f"{key} ready" + (f" (retry, attempt {p.get('next_attempt')})" if p.get("retry") else ""),
        "task.started": f"{key} started (attempt {p.get('attempt_number', p.get('attempt'))}) on {p.get('worker_id')}",
        "task.succeeded": f"{key} succeeded (attempt {p.get('attempt')}, {p.get('latency_ms')} ms, "
                          f"via {_via(p)}{_fallback(p)})",
        "task.failed": f"{key} failed: {p.get('error_type')} ({p.get('reason', '')})",
        "task.retry_scheduled": f"{key} attempt {p.get('attempt')} failed ({p.get('error_type')}); "
                                f"retry in {p.get('delay')}s",
        "task.lease_expired": f"{key} lost its worker {p.get('previous_owner')} ({p.get('reason')})",
        "task.interrupted": f"{key} interrupted{who}",
        "task.cancelled": f"{key} cancelled{who}",
        "task.skipped": f"{key} skipped (upstream {p.get('upstream')} rejected)",
        "approval.requested": (f"{key} wants to use {p.get('tool')}: approval requested"
                               + (" (read untrusted content)" if p.get("tainted") else "")
                               if p.get("kind") == "tool_call" else f"Approval requested for {key}"),
        "task.sources_verified": f"{key} sources checked by code: {p.get('verified')} verified, "
                                 f"{p.get('removed')} removed (never opened)",
        "tool.injection_suspected": f"{key}: suspicious text in {p.get('source')} "
                                    f"({', '.join(p.get('rules') or [])}); flagged, not followed",
        "approval.approved": f"{key} approved{who}"
                             + (f", input changed: {sorted(p['input_changes'])}" if p.get("input_changes") else ""),
        "approval.rejected": f"{key} rejected{who} ({p.get('on_reject')}){why}",
        "approval.expired": f"Approval for {key} expired",
        "workflow.budget_exceeded": f"Budget reached: {p.get('limit')}"
                                    + (" (estimated, before the call)" if p.get("estimated") else "")
                                    + f"; {(p.get('usage') or {}).get('llm_calls')} LLM calls, "
                                      f"${(p.get('usage') or {}).get('cost_usd', 0):.4f}: paused for a human",
        "workflow.budget_updated": f"Budget updated{who}: {p.get('to')}",
    }
    if event_type in texts:
        return texts[event_type]
    subject = key or ("Workflow" if event_type.startswith("workflow.") else event_type)
    return f"{subject} {p.get('from', '?')} -> {p.get('to', '?')}{why}"


async def get_timeline(session: AsyncSession, workflow_id: uuid.UUID) -> dict[str, Any]:
    wf = await get_workflow(session, workflow_id)
    origin = wf.created_at
    events = (await session.scalars(
        select(WorkflowEvent).where(WorkflowEvent.workflow_id == workflow_id).order_by(WorkflowEvent.id)
    )).all()
    attempts = (await session.scalars(
        select(TaskAttempt).where(TaskAttempt.workflow_id == workflow_id)
        .order_by(TaskAttempt.task_id, TaskAttempt.attempt_number)
    )).all()

    by_task: dict[uuid.UUID, list[TaskAttempt]] = {}
    for a in attempts:
        by_task.setdefault(a.task_id, []).append(a)
    spans = [
        {
            "task_key": t.key, "agent_type": t.agent_type, "status": t.status,
            "depends_on": [d.key for d in t.dependencies],
            "fallback_from": (t.metrics or {}).get("fallback_from"),
            "cost_usd": (t.metrics or {}).get("cost_usd"),
            "attempts": [
                {"attempt_number": a.attempt_number, "outcome": a.outcome, "worker_id": a.worker_id,
                 "error_type": a.error_type, "model": a.model, "provider": a.provider, "cost_usd": a.cost_usd,
                 "start_offset_s": _offset(a.started_at, origin), "end_offset_s": _offset(a.finished_at, origin),
                 "duration_s": round((a.finished_at - a.started_at).total_seconds(), 2) if a.finished_at else None}
                for a in by_task.get(t.id, [])
            ],
        }
        for t in wf.tasks
    ]
    tool_rows = (await session.scalars(select(ToolCall).where(ToolCall.workflow_id == workflow_id)
                                       .order_by(ToolCall.created_at))).all()
    tools_by_attempt: dict[tuple[uuid.UUID, int], list[dict[str, Any]]] = {}
    for r in tool_rows:
        tools_by_attempt.setdefault((r.task_id, r.attempt_number), []).append(
            {"tool": r.tool, "status": r.status, "offset_s": _offset(r.started_at or r.created_at, origin),
             "reused": r.reused_from_id is not None, "tainted": r.tainted_context})
    for span, t in zip(spans, wf.tasks, strict=True):
        for a in span["attempts"]:
            a["tool_calls"] = tools_by_attempt.get((t.id, a["attempt_number"]), [])
    calls = (await session.scalars(select(LLMCall).where(LLMCall.workflow_id == workflow_id))).all()
    cost_by_provider: dict[str, float | None] = {}
    for c in calls:  # failed calls cost nothing; one unpriced success makes that provider's total unknown
        current = cost_by_provider.setdefault(c.provider, 0.0)
        if c.outcome == "succeeded":
            cost_by_provider[c.provider] = None if current is None or c.cost_usd is None else round(
                current + c.cost_usd, 6)
    outcomes = Counter(a.outcome for a in attempts)
    types = Counter(e.event_type for e in events)
    summary = {
        "tasks": len(wf.tasks),
        "attempts": len(attempts),
        "attempt_outcomes": dict(outcomes),
        "retries_scheduled": types["task.retry_scheduled"],
        "restarts_recovered": types["engine.recovered"],
        "approvals": dict(Counter(a.status for a in wf.approvals)),
        "input_tokens": sum(a.input_tokens or 0 for a in attempts),
        "output_tokens": sum(a.output_tokens or 0 for a in attempts),
        "models": sorted({a.model for a in attempts if a.model}),
        "llm_calls": len(calls),
        "fallbacks": sum(1 for c in calls if c.outcome == "succeeded" and c.fallback_from),
        "cost_by_provider": cost_by_provider,
        "cost_usd": round(sum(c.cost_usd or 0 for c in calls), 6),
        # Part 9 tools
        "tool_calls_by_tool": dict(Counter(r.tool for r in tool_rows if r.status != "rejected_invalid")),
        "tool_calls_reused": sum(1 for r in tool_rows if r.reused_from_id is not None),
        "tool_approvals": dict(Counter(a.status for a in wf.approvals if a.kind == "tool_call")),
        "sources_verified": sum(e.payload.get("verified", 0) for e in events if e.event_type == "task.sources_verified"),
        "sources_removed": sum(e.payload.get("removed", 0) for e in events if e.event_type == "task.sources_verified"),
        "injection_flags": types["tool.injection_suspected"],
    }
    end = wf.completed_at
    return {
        "workflow_id": wf.id, "status": wf.status, "goal": wf.goal, "created_at": origin,
        "started_at": wf.started_at, "completed_at": end,
        "duration_seconds": round((end - wf.started_at).total_seconds(), 2) if end and wf.started_at else None,
        "summary": summary,
        "spans": spans,
        "entries": [
            {"offset_s": _offset(e.created_at, origin), "at": e.created_at, "event_type": e.event_type,
             "task_key": e.payload.get("task_key") or e.payload.get("key"), "text": describe(e.event_type, e.payload)}
            for e in events
        ],
    }
