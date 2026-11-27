"""Approval checkpoints (Part 6). Code decides control: a requires_approval task runs only after a
human approved it, and the engine re-checks that at claim time.

Lifecycle of one approval row:
    (scheduler) READY task, no approved row -> pending (task AWAITING_APPROVAL)
    pending --approve--> approved   (task READY; valid for all automatic retries of the task)
    pending --reject---> rejected   (task FAILED 'rejected' + workflow FAILED, or task + descendants SKIPPED)
    pending --timeout--> expired    (task FAILED 'approval_expired' + workflow FAILED)
    pending --cancel---> cancelled  (workflow cancelled)
Lock order everywhere: workflow row, then approval row, then tasks (same as the scheduler).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.registry import sensitive_agent_types
from app.config import Settings, get_settings
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.events import EventType, record_event
from app.core.state_machine import transition_task, transition_workflow
from app.db.base import utcnow
from app.models.approval import Approval
from app.models.task import Task
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 1500
_FAILABLE = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.PAUSED)


def _preview(output: dict[str, Any] | None) -> str:
    text = json.dumps(output or {}, ensure_ascii=False)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + f"... [truncated, {len(text)} chars]"


def build_request_payload(wf: Workflow, task: Task) -> dict[str, Any]:
    """Everything a human needs to decide, frozen at request time. Task deps must be loaded."""
    sensitive = task.agent_type in sensitive_agent_types()
    return {
        "workflow_id": str(wf.id),
        "workflow_goal": wf.goal,
        "task_key": task.key,
        "task_name": task.name,
        "task_description": task.description,
        "agent_type": task.agent_type,
        "why_sensitive": (f"Agent '{task.agent_type}' is marked sensitive: its tasks always need approval."
                          if sensitive else "The task was created with requires_approval=true."),
        "task_input": task.input,
        "dependency_previews": {d.key: _preview(d.output) for d in task.dependencies},
    }


async def approved_task_ids(session: AsyncSession, workflow_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(Approval.task_id).where(Approval.workflow_id == workflow_id, Approval.status == "approved",
                                       Approval.kind == "task")
    )
    return set(rows.all())


# ------------------------------------------------------------------------------ Part 11 governance


async def team_name(session: AsyncSession, wf: Workflow) -> str | None:
    if wf.team_id is None:
        return None
    from app.models.governance import Team

    return await session.scalar(select(Team.name).where(Team.id == wf.team_id))


async def _apply_policy(session: AsyncSession, wf: Workflow, approval: Approval, **ctx: Any) -> None:
    """Stamp the policy outcome on a new approval: how many distinct approvers, which role, which rules."""
    from app.governance.policy import get_policies

    req = get_policies().evaluate(team=await team_name(session, wf), **ctx)
    approval.required_approvals = req.approvals
    approval.approver_role = req.approver_role
    approval.policy_rules = req.rules
    if req.rules:
        approval.request_payload = {**(approval.request_payload or {}), "policy": {
            "rules": req.rules, "required_approvals": req.approvals, "approver_role": req.approver_role}}


def _voter(actor: str) -> str:
    """The identity a vote counts for: a user is themselves; the admin key is ONE voter whatever actor it names."""
    from app.governance.principal import current

    p = current()
    return p.name or actor if p.kind == "user" else ("admin_key" if p.kind == "admin_key" else actor)


def _authorize(approval: Approval, wf: Workflow, actor: str, *, voting: bool) -> None:
    """Safety floor 4-5: the approver role, one vote per person, no self-approval when two people are required."""
    from app.core.errors import ForbiddenError
    from app.governance.principal import current

    p = current()
    if approval.approver_role and not p.at_least(approval.approver_role):
        raise ForbiddenError(f"This approval needs role {approval.approver_role} (yours: {p.role})")
    if not voting or approval.required_approvals <= 1:
        return
    voter = _voter(actor)
    if p.kind == "user" and wf.created_by and wf.created_by == p.name:
        raise ForbiddenError("Four-eyes rule: the person who created this workflow cannot approve it")
    if any(v.get("voter") == voter for v in approval.votes or []):
        raise ConflictError(f"{voter} already approved; it needs {approval.required_approvals} different people")


async def _record_vote(session: AsyncSession, approval: Approval, wf: Workflow, actor: str, comment: str | None,
                       overrides: dict[str, Any]) -> bool:
    """Add a vote. True = enough distinct approvers now. Edited content resets earlier votes (everyone approves
    exactly what will run)."""
    votes = list(approval.votes or [])
    edited = any(v for v in overrides.values())
    if edited and votes:
        votes = []
    votes.append({"voter": _voter(actor), "actor": actor, "at": utcnow().isoformat(), "comment": comment,
                  **({"overrides": overrides} if edited else {})})
    approval.votes = votes
    done = len(votes) >= approval.required_approvals
    if not done:
        record_event(session, workflow_id=wf.id, task_id=approval.task_id, event_type=EventType.APPROVAL_VOTE_RECORDED,
                     payload={"approval_id": str(approval.id), "actor": actor, "votes": len(votes),
                              "task_key": (approval.request_payload or {}).get("task_key"),
                              "required": approval.required_approvals, "reset_earlier_votes": edited and len(votes) == 1,
                              "policy_rules": approval.policy_rules})
    return done


async def _recheck_edited_call(session: AsyncSession, wf: Workflow, task: Task, approval: Approval,
                               overrides: dict[str, Any]) -> None:
    """The approver's edited arguments go through the policy again: a deny refuses the edit; a stricter rule raises
    the number of approvers / the role, and earlier votes no longer count (they approved different content)."""
    from app.core.errors import ForbiddenError
    from app.governance.policy import get_policies, recipients_of

    payload = approval.request_payload or {}
    final = {**(payload.get("arguments") or {}), **overrides}
    req = get_policies().evaluate(scope="tool", team=await team_name(session, wf), agent_type=task.agent_type,
                                  task_key=task.base_key or task.key, tool=payload.get("tool"),
                                  side_effect=payload.get("side_effect"), recipients=recipients_of(final))
    if req.deny:
        await session.rollback()
        raise ForbiddenError(f"These arguments are denied by policy ({', '.join(req.rules)})")
    from app.governance.principal import ROLE_RANK

    stricter_role = req.approver_role and (approval.approver_role is None
                                           or ROLE_RANK[req.approver_role] > ROLE_RANK[approval.approver_role])
    if req.approvals > approval.required_approvals or stricter_role:
        approval.required_approvals = max(approval.required_approvals, req.approvals)
        if stricter_role:
            approval.approver_role = req.approver_role
        approval.policy_rules = sorted(set(approval.policy_rules or []) | set(req.rules))
        approval.votes = []
        _authorize(approval, wf, _voter_name(), voting=True)  # the editor must still be allowed under the new rule


def _voter_name() -> str:
    from app.governance.principal import current

    return current().name or "admin"


async def request_approval(session: AsyncSession, wf: Workflow, task: Task, settings: Settings) -> Approval:
    """READY gated task -> AWAITING_APPROVAL + a pending approval (event approval.requested)."""
    now = utcnow()
    hours = settings.approval_timeout_hours
    approval = Approval(
        id=uuid.uuid4(), workflow_id=wf.id, task_id=task.id, status="pending", requested_at=now,
        expires_at=now + timedelta(hours=hours) if hours > 0 else None,
        request_payload=build_request_payload(wf, task),
    )
    await _apply_policy(session, wf, approval, scope="task", agent_type=task.agent_type,
                        task_key=task.base_key or task.key)
    session.add(approval)
    await transition_task(
        session, task, TaskStatus.AWAITING_APPROVAL, event_type=EventType.APPROVAL_REQUESTED,
        extra={"approval_id": str(approval.id),
               "expires_at": approval.expires_at.isoformat() if approval.expires_at else None},
    )
    logger.info("approval.requested workflow=%s task=%s approval=%s", wf.id, task.key, approval.id)
    return approval


async def request_tool_approval(session: AsyncSession, wf: Workflow, task: Task, tool_call_id: uuid.UUID,
                                payload: dict[str, Any], settings: Settings) -> Approval:
    """RUNNING task suspended on one proposed tool call -> AWAITING_APPROVAL + a pending kind=tool_call approval."""
    from app.models.tool_call import ToolCall

    now = utcnow()
    hours = settings.approval_timeout_hours
    approval = Approval(
        id=uuid.uuid4(), workflow_id=wf.id, task_id=task.id, status="pending", requested_at=now, kind="tool_call",
        tool_call_id=tool_call_id, expires_at=now + timedelta(hours=hours) if hours > 0 else None,
        request_payload={**payload, "workflow_goal": wf.goal, "task_key": task.key, "task_name": task.name,
                         "agent_type": task.agent_type},
    )
    from app.governance.policy import recipients_of

    await _apply_policy(session, wf, approval, scope="tool", agent_type=task.agent_type,
                        task_key=task.base_key or task.key, tool=payload.get("tool"), side_effect=payload.get("side_effect"),
                        recipients=recipients_of(payload.get("arguments") or {}))
    session.add(approval)
    await session.flush()
    call = await session.get(ToolCall, tool_call_id)
    if call is not None:
        call.approval_id = approval.id
    await transition_task(
        session, task, TaskStatus.AWAITING_APPROVAL, event_type=EventType.APPROVAL_REQUESTED, reason="tool_call",
        extra={"approval_id": str(approval.id), "kind": "tool_call", "tool": payload.get("tool"),
               "tainted": payload.get("tainted", False),
               "expires_at": approval.expires_at.isoformat() if approval.expires_at else None},
    )
    logger.info("approval.requested workflow=%s task=%s approval=%s tool=%s", wf.id, task.key, approval.id,
                payload.get("tool"))
    return approval


async def request_loop_approval(session: AsyncSession, wf: Workflow, spec: Any, iteration: int, reason: str,
                                tasks: Sequence[Task]) -> Approval:
    """A loop used up its iterations and its spec says ask_human: a workflow-level approval (no task)."""
    from app.services import graph_service

    now = utcnow()
    hours = get_settings().approval_timeout_hours
    reviews = {}
    for i in range(1, iteration + 1):
        out = graph_service.iteration_tasks(tasks, spec.name, i).get(spec.feedback.from_)
        reviews[str(i)] = {"score": (out.output or {}).get("score") if out else None,
                           "summary": (out.output or {}).get("summary") if out else None}
    approval = Approval(
        id=uuid.uuid4(), workflow_id=wf.id, task_id=None, kind="loop_exhausted", status="pending", requested_at=now,
        expires_at=now + timedelta(hours=hours) if hours > 0 else None,
        request_payload={"kind": "loop_exhausted", "loop": spec.name, "iterations": iteration, "reason": reason,
                         "until": spec.until, "reviews": reviews, "workflow_goal": wf.goal,
                         "options": "approve = continue with the best iteration; approve with extra_iterations = try "
                                    "again; reject = fail the workflow"},
    )
    session.add(approval)
    await session.flush()
    record_event(session, workflow_id=wf.id, event_type=EventType.APPROVAL_REQUESTED,
                 payload={"approval_id": str(approval.id), "kind": "loop_exhausted", "loop": spec.name})
    return approval


async def _decide_workflow_level(session: AsyncSession, approval: Approval, wf: Workflow, *, approved: bool, actor: str,
                                 comment: str | None, extra_iterations: int | None = None) -> Approval:
    """Decisions on approvals that belong to the workflow, not a task (loop_exhausted; plan_patch in Part 10)."""
    payload = approval.request_payload or {}
    approval.status = "approved" if approved else "rejected"
    approval.decided_at, approval.decided_by, approval.comment = utcnow(), actor, comment
    extra = {"approval_id": str(approval.id), "actor": actor, "comment": comment, "kind": approval.kind}
    if approval.kind == "loop_exhausted":
        loops = dict((wf.context or {}).get("loops", {}))
        name = payload["loop"]
        state = dict(loops.get(name, {}))
        if approved and extra_iterations:
            state.update(status="expand_pending", extra_iterations=int(state.get("extra_iterations", 0)) + extra_iterations)
            extra["extra_iterations"] = extra_iterations
        elif approved:
            state.update(status="choose_best", reason=f"approved by {actor}: continue with the best iteration")
        else:
            state.update(status="exhausted", reason=f"rejected by {actor}")
        loops[name] = state
        wf.context = {**wf.context, "loops": loops}
    elif approval.kind == "plan_patch":
        from app.services import replan_service

        await replan_service.decide_patch(session, wf, approval, approved=approved, actor=actor)
    record_event(session, workflow_id=wf.id,
                 event_type=EventType.APPROVAL_APPROVED if approved else EventType.APPROVAL_REJECTED, payload=extra)
    if not approved and wf.status in _FAILABLE and approval.kind == "loop_exhausted":
        wf.error = f"Loop '{payload.get('loop')}' stopped by {actor}: {comment or 'rejected'}"
        await transition_workflow(session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
                                  reason=wf.error)
    elif wf.status == WorkflowStatus.AWAITING_APPROVAL:
        await transition_workflow(session, wf, WorkflowStatus.RUNNING, reason=f"decided by {actor}")
    await session.commit()
    logger.info("approval.%s kind=%s approval=%s actor=%s", approval.status, approval.kind, approval.id, actor)
    return approval


async def _set_tool_call(session: AsyncSession, approval: Approval, status: str, **values: Any) -> None:
    from app.models.tool_call import ToolCall

    if approval.tool_call_id is None:
        return
    call = await session.get(ToolCall, approval.tool_call_id, with_for_update=True)
    if call is not None and call.status == "awaiting_approval":
        call.status = status
        for k, v in values.items():
            setattr(call, k, v)


async def expire_due(session: AsyncSession, wf: Workflow, tasks: list[Task], *,
                     workflow_level_only: bool = False) -> int:
    """Pending approvals past expires_at -> expired; their task FAILED (approval_expired)."""
    stmt = select(Approval).where(Approval.workflow_id == wf.id, Approval.status == "pending",
                                  Approval.expires_at.is_not(None), Approval.expires_at <= utcnow())
    if workflow_level_only:
        stmt = stmt.where(Approval.task_id.is_(None))
    rows = (await session.scalars(stmt.with_for_update())).all()
    by_id = {t.id: t for t in tasks}
    for a in rows:
        a.status, a.decided_at = "expired", utcnow()
        if a.task_id is None:  # workflow-level approval (loop_exhausted / plan_patch): nobody decided in time
            record_event(session, workflow_id=wf.id, event_type=EventType.APPROVAL_EXPIRED,
                         payload={"approval_id": str(a.id), "kind": a.kind})
            if a.kind == "plan_patch":
                from app.services import replan_service

                # the re-plan's own failure rule decides: automatic trigger -> FAILED, human -> back to before
                await replan_service.decide_patch(session, wf, a, approved=False, actor="expiry")
                continue
            if wf.status in _FAILABLE:
                wf.error = f"{a.kind} approval {a.id} expired without a decision"
                await transition_workflow(session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
                                          reason=wf.error)
            continue
        await _set_tool_call(session, a, "denied", error="approval expired")
        task = by_id[a.task_id]
        task.error_type = "approval_expired"
        task.error = f"Approval {a.id} expired at {a.expires_at.isoformat()} without a decision"
        if task.status == TaskStatus.AWAITING_APPROVAL:
            await transition_task(session, task, TaskStatus.FAILED, event_type=EventType.APPROVAL_EXPIRED,
                                  extra={"approval_id": str(a.id)})
        logger.warning("approval.expired workflow=%s task=%s", wf.id, task.key)
    return len(rows)


# ------------------------------------------------------------------------------ queries


async def list_approvals(
    session: AsyncSession, status: str | None = None, workflow_id: uuid.UUID | None = None
) -> list[Approval]:
    stmt = select(Approval).order_by(Approval.requested_at, Approval.id)
    if status:
        stmt = stmt.where(Approval.status == status)
    if workflow_id:
        stmt = stmt.where(Approval.workflow_id == workflow_id)
    from app.governance.access import workflow_filters

    if team := workflow_filters():  # Part 11: only the caller's team
        stmt = stmt.join(Workflow, Workflow.id == Approval.workflow_id).where(*team)
    return list((await session.scalars(stmt)).all())


async def get_approval(session: AsyncSession, approval_id: uuid.UUID) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    return approval


# ------------------------------------------------------------------------------ decisions


async def _lock_pending(session: AsyncSession, approval_id: uuid.UUID) -> tuple[Approval, Workflow, Task]:
    """Lock workflow -> approval -> task; 409 unless the approval is still pending and unexpired.

    The row locks serialize concurrent decisions: the second caller sees a decided row and gets 409.
    """
    wf_id = await session.scalar(select(Approval.workflow_id).where(Approval.id == approval_id))
    if wf_id is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    wf = await session.scalar(select(Workflow).where(Workflow.id == wf_id).with_for_update())
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    task = await session.scalar(
        select(Task).where(Task.id == approval.task_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval.status != "pending":
        status = approval.status
        await session.rollback()
        raise ConflictError(f"Approval is already {status}")
    if approval.expires_at is not None and approval.expires_at <= utcnow():
        await session.rollback()
        raise ConflictError("Approval has expired")
    return approval, wf, task


APPROVE_FIELDS: dict[str, set[str]] = {
    "task": {"actor", "comment", "input_overrides"},
    "tool_call": {"actor", "comment", "arguments_overrides", "allow_recipient_override", "confirm_duplicate"},
    "plan_patch": {"actor", "comment"},
    "loop_exhausted": {"actor", "comment", "extra_iterations"},
}


async def check_approve_fields(session: AsyncSession, approval_id: uuid.UUID, fields: set[str]) -> None:
    """422 when the body carries fields that do not apply to this approval's kind (never silently ignored)."""
    approval = await get_approval(session, approval_id)
    allowed = APPROVE_FIELDS.get(approval.kind, {"actor", "comment"})
    extra = sorted(fields - allowed)
    if extra:
        raise ValidationFailedError(
            f"Field(s) {', '.join(extra)} do not apply to a {approval.kind} approval; allowed: {', '.join(sorted(allowed))}")


async def duplicate_of(session: AsyncSession, approval: Approval, overrides: dict[str, Any] | None) -> tuple[str, Any]:
    """(idempotency key of the arguments that would run, the time an identical call already succeeded or None)."""
    import app.tools.catalog  # noqa: F401
    from app.models.tool_call import ToolCall
    from app.tools import journal
    from app.tools.registry import get_tool

    call = await session.get(ToolCall, approval.tool_call_id) if approval.tool_call_id else None
    if call is None:
        return "", None
    final_args = {**(call.arguments or {}), **(overrides or {})}
    try:
        final_args = get_tool(call.tool).input_model.model_validate(final_args).model_dump(mode="json")
    except Exception:
        return "", None
    key = journal.idempotency_key(call.workflow_id, approval.request_payload.get("task_key", ""), call.tool,
                                  journal.args_hash(final_args))
    sent = await session.scalar(select(ToolCall.finished_at).where(
        ToolCall.idempotency_key == key, ToolCall.status == "succeeded", ToolCall.id != call.id)
        .order_by(ToolCall.finished_at).limit(1))
    return key, sent


async def approve(
    session: AsyncSession, approval_id: uuid.UUID, *, actor: str, comment: str | None,
    input_overrides: dict[str, Any] | None, arguments_overrides: dict[str, Any] | None = None,
    allow_recipient_override: bool = False, confirm_duplicate: bool = False, extra_iterations: int | None = None,
) -> Approval:
    approval, wf, task = await _lock_pending(session, approval_id)
    _authorize(approval, wf, actor, voting=True)
    if approval.kind == "tool_call" and arguments_overrides:
        await _recheck_edited_call(session, wf, task, approval, arguments_overrides)
    if approval.required_approvals > 1:
        given = {"input_overrides": input_overrides, "arguments_overrides": arguments_overrides,
                 "allow_recipient_override": allow_recipient_override}
        if not await _record_vote(session, approval, wf, actor, comment, given):
            await session.commit()
            logger.info("approval.vote approval=%s actor=%s votes=%d/%d", approval.id, actor, len(approval.votes),
                        approval.required_approvals)
            return approval
        first = (approval.votes or [{}])[0].get("overrides") or {}  # the content everyone approved
        input_overrides = first.get("input_overrides") or input_overrides
        arguments_overrides = first.get("arguments_overrides") or arguments_overrides
        allow_recipient_override = bool(first.get("allow_recipient_override") or allow_recipient_override)
    if approval.task_id is None:
        return await _decide_workflow_level(session, approval, wf, approved=True, actor=actor, comment=comment,
                                            extra_iterations=extra_iterations)
    changes: dict[str, Any] = {}
    if approval.kind == "tool_call":
        _, sent_at = await duplicate_of(session, approval, arguments_overrides)
        if sent_at is not None and not confirm_duplicate:  # never a silent duplicate send
            await session.rollback()
            raise ConflictError(f"This exact message was already sent at {sent_at.isoformat()}. Approve again with "
                                "confirm_duplicate=true to send it anyway.")
        changes = await _approve_tool_call(session, approval, arguments_overrides, allow_recipient_override)
    elif input_overrides:
        old = task.input or {}
        changes = {k: {"from": old.get(k), "to": v} for k, v in input_overrides.items() if old.get(k) != v}
        task.input = {**old, **input_overrides}  # shallow merge; reassign so the JSONB change is saved
    approval.status, approval.decided_at, approval.decided_by = "approved", utcnow(), actor
    if approval.kind != "tool_call":
        approval.comment, approval.input_overrides = comment, input_overrides or None
    else:
        approval.comment = comment
    extra = {"approval_id": str(approval.id), "actor": actor, "comment": comment, "input_changes": changes,
             "kind": approval.kind, **({"confirm_duplicate": True} if confirm_duplicate else {})}
    if task.status == TaskStatus.AWAITING_APPROVAL:
        await transition_task(session, task, TaskStatus.READY, event_type=EventType.APPROVAL_APPROVED,
                              extra=extra)
    if wf.status == WorkflowStatus.AWAITING_APPROVAL:  # PAUSED stays PAUSED; the decision is kept
        await transition_workflow(session, wf, WorkflowStatus.RUNNING, reason=f"approved by {actor}")
    await session.commit()
    logger.info("approval.approved approval=%s task=%s actor=%s changed=%s", approval.id, task.key, actor,
                sorted(changes))
    return approval


async def _approve_tool_call(session: AsyncSession, approval: Approval, overrides: dict[str, Any] | None,
                             allow_recipient_override: bool) -> dict[str, Any]:
    """Validate the (possibly edited) arguments against the tool's schema; the journal row gets the final
    arguments and their idempotency key (the key is computed on what actually executes)."""
    from pydantic import ValidationError

    import app.tools.catalog  # noqa: F401
    from app.models.tool_call import ToolCall
    from app.tools import journal
    from app.tools.registry import get_tool

    call = await session.get(ToolCall, approval.tool_call_id, with_for_update=True) if approval.tool_call_id else None
    if call is None:
        await session.rollback()
        raise ConflictError("The tool call for this approval no longer exists")
    original = dict(call.arguments or {})
    final_args = {**original, **(overrides or {})} if overrides is not None else original
    try:
        validated = get_tool(call.tool).input_model.model_validate(final_args).model_dump(mode="json")
    except ValidationError as exc:
        tool_name = call.tool
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:6])
        await session.rollback()
        raise ValidationFailedError(f"The edited arguments are not valid for {tool_name}: {problems}") from exc
    changes = {k: {"from": original.get(k), "to": v} for k, v in validated.items() if original.get(k) != v}
    if changes:
        call.proposed_arguments = original
        call.arguments = validated
        call.args_hash = journal.args_hash(validated)
        call.idempotency_key = journal.idempotency_key(call.workflow_id, approval.request_payload.get("task_key", ""),
                                                       call.tool, call.args_hash)
    call.status = "approved"
    approval.input_overrides = {"arguments": validated, "flags": {"allow_recipient_override": allow_recipient_override}}
    return changes


async def default_on_reject(session: AsyncSession, approval_id: uuid.UUID) -> str:
    """Task approvals default to fail_workflow; tool calls to the tool's tools.yaml on_reject."""
    approval = await get_approval(session, approval_id)
    if approval.task_id is None:
        return "fail_workflow"  # workflow-level approvals: reject = stop (loop) / discard the patch
    if approval.kind != "tool_call":
        return "fail_workflow"
    from app.tools import config as tools_config

    cfg = tools_config.get_tools_config()
    entry = cfg.entry(approval.request_payload.get("tool", "")) if cfg else None
    return entry.on_reject if entry else "inform_agent"


async def reject(
    session: AsyncSession, approval_id: uuid.UUID, *, actor: str, reason: str, on_reject: str
) -> Approval:
    approval, wf, task = await _lock_pending(session, approval_id)
    _authorize(approval, wf, actor, voting=False)  # one eligible veto rejects (floor 6)
    if approval.task_id is None:
        return await _decide_workflow_level(session, approval, wf, approved=False, actor=actor, comment=reason)
    if approval.kind == "tool_call" and on_reject not in ("inform_agent", "fail_task"):
        await session.rollback()
        raise ValidationFailedError("A tool-call approval is rejected with on_reject inform_agent or fail_task")
    if approval.kind != "tool_call" and on_reject not in ("fail_workflow", "skip_task"):
        await session.rollback()
        raise ValidationFailedError("A task approval is rejected with on_reject fail_workflow or skip_task")
    approval.status, approval.decided_at, approval.decided_by = "rejected", utcnow(), actor
    approval.comment, approval.on_reject = reason, on_reject
    extra = {"approval_id": str(approval.id), "actor": actor, "reason": reason, "on_reject": on_reject}
    if approval.kind == "tool_call":
        await _set_tool_call(session, approval, "denied", error=reason, finished_at=utcnow())
    if on_reject == "inform_agent":
        # The agent hears "denied by human: <reason>" on its next step and adapts (e.g. finishes without sending).
        await transition_task(session, task, TaskStatus.READY, event_type=EventType.APPROVAL_REJECTED, extra=extra)
        if wf.status == WorkflowStatus.AWAITING_APPROVAL:
            await transition_workflow(session, wf, WorkflowStatus.RUNNING, reason=f"tool call denied by {actor}")
        logger.info("approval.rejected task=%s inform_agent", task.key)
    elif on_reject == "skip_task":
        tasks = (await session.scalars(
            select(Task).where(Task.workflow_id == wf.id).options(selectinload(Task.dependencies))
            .with_for_update()
        )).all()
        await transition_task(session, task, TaskStatus.SKIPPED, event_type=EventType.APPROVAL_REJECTED,
                              extra=extra)
        # Descendants cannot have started (their upstream never ran), so they are PENDING.
        targets = [t for t in _descendants(task, tasks) if t.status == TaskStatus.PENDING]
        for t in targets:
            await transition_task(session, t, TaskStatus.SKIPPED, event_type=EventType.TASK_SKIPPED,
                                  extra={"reason": "upstream_rejected", "upstream": task.key})
        skipped = [t.key for t in targets]
        if wf.status == WorkflowStatus.AWAITING_APPROVAL:
            await transition_workflow(session, wf, WorkflowStatus.RUNNING,
                                      reason=f"task '{task.key}' skipped after rejection")
        logger.info("approval.rejected task=%s skip_task descendants=%s", task.key, skipped)
    else:
        task.error_type = "rejected"
        task.error = f"Rejected by {actor}: {reason}"
        await transition_task(session, task, TaskStatus.FAILED, event_type=EventType.APPROVAL_REJECTED,
                              extra=extra)
        if wf.status in _FAILABLE:
            wf.error = f"Task '{task.key}' failed (rejected): {task.error}"
            await transition_workflow(session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
                                      reason=wf.error, extra={"failed_tasks": [task.key]})
        logger.info("approval.rejected task=%s fail_workflow", task.key)
    await session.commit()
    return approval


def _descendants(root: Task, tasks: list[Task]) -> list[Task]:
    children: dict[uuid.UUID, list[Task]] = {}
    for t in tasks:
        for d in t.dependencies:
            children.setdefault(d.id, []).append(t)
    seen: dict[uuid.UUID, Task] = {}
    stack = list(children.get(root.id, []))
    while stack:
        t = stack.pop()
        if t.id not in seen:
            seen[t.id] = t
            stack.extend(children.get(t.id, []))
    return list(seen.values())
