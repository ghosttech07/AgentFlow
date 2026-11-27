"""Re-planning mid-run (Part 10): bounded, validated, auditable, crash-safe.

Triggers: (a) a task FAILED for good while the workflow opted in (`context.adaptive.replan_on_failure`);
(b) a succeeded task's `replan_if` is true; (c) a human POST /workflows/{id}/replan (any workflow).
Flow: request() -> REPLANNING (state "requested", committed) -> the engine runs `run_job` (state "planning") ->
the planner returns a PlanPatch -> code validates (repair loop) -> auto-apply, or a plan_patch approval (state
"proposed") -> apply() in one locked transaction (plan_versions row, SUPERSEDED, rewired dependents) -> back to the
previous status. Finished, running or approved work is never undone.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.errors import ConflictError, ValidationFailedError
from app.core.events import EventType, record_event
from app.core.plan_patch import PlanPatch, patch_diff, validate_patch
from app.core.state_machine import transition_task, transition_workflow
from app.db.base import utcnow
from app.llm.base import LLMClient
from app.models.adaptive import PlanVersion
from app.models.approval import Approval
from app.models.task import Task
from app.models.workflow import Workflow
from app.services import graph_service

logger = logging.getLogger(__name__)
REPLANNABLE = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.PAUSED, WorkflowStatus.FAILED)
AUTO_TRIGGERS = ("task_failed", "replan_if")


def replans_done(wf: Workflow) -> int:
    return int(((wf.context or {}).get("adaptive") or {}).get("replans", 0))


def max_replans(wf: Workflow, settings: Settings) -> int:
    budget = wf.budget or {}
    return int(budget["max_replans"]) if budget.get("max_replans") is not None else settings.max_replans


def can_replan(wf: Workflow, settings: Settings) -> bool:
    return replans_done(wf) < max_replans(wf, settings)


async def request(session: AsyncSession, wf: Workflow, *, trigger: str, reason: str, actor: str = "engine",
                  instructions: str | None = None, failed_task: str | None = None) -> None:
    """Move a locked workflow to REPLANNING (no commit). The engine picks up state "requested"."""
    previous = str(wf.status)
    wf.context = {**(wf.context or {}), "replan": {
        "state": "requested", "previous_status": previous, "trigger": trigger, "reason": reason, "actor": actor,
        "instructions": instructions, "failed_task": failed_task, "requested_at": utcnow().isoformat()}}
    await transition_workflow(session, wf, WorkflowStatus.REPLANNING, event_type=EventType.WORKFLOW_REPLAN_STARTED,
                              reason=reason, extra={"trigger": trigger, "actor": actor, "failed_task": failed_task,
                                                    "previous_status": previous})
    logger.info("workflow.replan_started id=%s trigger=%s reason=%s", wf.id, trigger, reason)


async def request_human(session: AsyncSession, workflow_id: uuid.UUID, actor: str, instructions: str | None) -> Workflow:
    """POST /workflows/{id}/replan: works on every workflow (manual ones too), within MAX_REPLANS."""
    wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if wf is None:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Workflow {workflow_id} not found")
    if wf.status not in REPLANNABLE:
        raise ConflictError(f"A {wf.status} workflow cannot be re-planned")
    if not can_replan(wf, get_settings()):
        raise ConflictError(f"This workflow already used its {max_replans(wf, get_settings())} re-plans")
    await request(session, wf, trigger="human", reason=instructions or "requested by a human", actor=actor,
                  instructions=instructions)
    await session.commit()
    return wf


async def claim_requested(session_factory: async_sessionmaker[AsyncSession], limit: int = 5) -> list[uuid.UUID]:
    """REPLANNING workflows waiting for the planner: mark them "planning" (SKIP LOCKED) and return their ids."""
    async with session_factory() as session:
        rows = list(await session.scalars(select(Workflow).where(Workflow.status == WorkflowStatus.REPLANNING)
                                          .with_for_update(skip_locked=True).limit(limit)))
        ids = []
        for wf in rows:
            rp = dict((wf.context or {}).get("replan") or {})
            if rp.get("state") == "requested":
                rp["state"] = "planning"
                wf.context = {**wf.context, "replan": rp}
                ids.append(wf.id)
        await session.commit()
        return ids


def _graph_prompt(wf: Workflow, tasks: list[Task], remaining: dict[str, Any]) -> str:
    rp = (wf.context or {}).get("replan") or {}
    rows = []
    for t in tasks:
        out = ""
        if t.status == TaskStatus.SUCCEEDED and t.output:
            out = " | " + json.dumps(t.output, ensure_ascii=False, default=str)[:300]
        if t.status == TaskStatus.FAILED:
            out = f" | error ({t.error_type}): {(t.error or '')[:200]}"
        rows.append(f"- {t.key} [{t.agent_type}] {t.status} after: {', '.join(d.key for d in t.dependencies) or 'start'}{out}")
    failed = [t.key for t in tasks if t.status == TaskStatus.FAILED]
    return (f"## GOAL\n{wf.goal}\n\n## WHY A RE-PLAN\n{rp.get('reason')}\n\n"
            + ("## FAILED TASKS\n" + "\n".join(f"FAILED_TASK: {k}" for k in failed) + "\n\n" if failed else "")
            + (f"## INSTRUCTIONS FROM A HUMAN\n{rp.get('instructions')}\n\n" if rp.get("instructions") else "")
            + "## CURRENT GRAPH (key [agent] status, dependencies, result summary)\n" + "\n".join(rows)
            + f"\n\n## REMAINING BUDGET\n{json.dumps(remaining)}\n")


PATCH_RULES = """You are re-planning a workflow that is already running. Return a PlanPatch:
- add_tasks: new tasks (same fields as a plan task; unique NEW keys; depends_on may use existing or new keys).
- cancel_tasks: only keys of tasks that have NOT started (PENDING/READY). Never cancel finished or running work.
- rewire: for each FAILED task you replace, {"from_failed": "<failed key>", "to_replacement": "<new key>"}; the
  replacement takes over everything that depended on the failed task.
Add as few tasks as possible (max {max_new}). Do not repeat work that already SUCCEEDED; use its results."""


async def run_job(session_factory: async_sessionmaker[AsyncSession], llm: LLMClient, workflow_id: uuid.UUID,
                  settings: Settings | None = None) -> None:
    """Ask the planner for a patch, validate it (repairs), then apply it or ask a human."""
    from app.agents.planner import PlannerAgent
    from app.agents.registry import task_agent_types
    from app.llm.context import CallContext, use_context
    from app.services import budget_service

    settings = settings or get_settings()
    async with session_factory() as session:
        wf = await session.get(Workflow, workflow_id)
        tasks = list(await session.scalars(select(Task).where(Task.workflow_id == workflow_id)
                                           .options(selectinload(Task.dependencies)).order_by(Task.created_at)))
        usage = await budget_service.get_usage(session, wf)
        remaining = budget_service.remaining(budget_service.effective_budget(wf.budget, settings), usage)
        system = PlannerAgent(llm).build_system_prompt() + "\n\n" + PATCH_RULES.replace(
            "{max_new}", str(settings.replan_max_new_tasks))
        prompt = _graph_prompt(wf, tasks, remaining)
    patch: PlanPatch | None = None
    errors: list[str] = []
    for _attempt in range(1 + settings.planner_max_repair_attempts):
        try:
            with use_context(CallContext(workflow_id)):
                resp = await llm.generate_structured(
                    system_prompt=system, user_prompt=prompt + (
                        "\n## YOUR PREVIOUS PATCH WAS INVALID\n" + "\n".join(f"- {e}" for e in errors) if errors else ""),
                    output_model=PlanPatch, thinking_level=settings.planner_thinking_level, role="planner")
        except Exception as exc:  # LLM unavailable: the re-plan fails cleanly
            errors = [f"planner call failed: {type(exc).__name__}: {exc}"]
            break
        candidate = resp.data
        errors = [e.message for e in validate_patch(
            candidate, tasks, [t for t in task_agent_types()], max_new_tasks=settings.replan_max_new_tasks,
            max_tasks=settings.max_plan_tasks + settings.replan_max_new_tasks * max(1, settings.max_replans),
            max_loop_iterations=settings.max_loop_iterations)]
        if not errors:
            patch = candidate
            break
    try:
        async with session_factory() as session:
            wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
            if wf is None or wf.status != WorkflowStatus.REPLANNING:
                return  # cancelled meanwhile: the patch is discarded
            if patch is None:
                await _fail(session, wf, errors)
                await session.commit()
                return
            await propose_or_apply(session, wf, patch, created_by="planner", settings=settings)
            await session.commit()
    except Exception as exc:  # the graph changed under the patch (or a bug): fail the re-plan, never hang in "planning"
        logger.exception("workflow.replan_apply_failed id=%s", workflow_id)
        async with session_factory() as session:
            wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
            if wf is not None and wf.status == WorkflowStatus.REPLANNING:
                await _fail(session, wf, [f"applying the patch failed: {type(exc).__name__}: {exc}"])
                await session.commit()


def _needs_approval(wf: Workflow, patch: PlanPatch, tasks: list[Task], settings: Settings) -> list[str]:
    from app.agents.registry import sensitive_agent_types

    why = []
    sensitive = set(sensitive_agent_types())
    if any(t.agent_type in sensitive or t.requires_approval for t in patch.add_tasks):
        why.append("it adds a sensitive step")
    if len(patch.add_tasks) > settings.replan_auto_approve_max_tasks:
        why.append(f"it adds {len(patch.add_tasks)} tasks (auto-approve limit {settings.replan_auto_approve_max_tasks})")
    failed_key = ((wf.context or {}).get("replan") or {}).get("failed_task")
    by_key = {t.key: t for t in tasks}
    if failed_key and failed_key in by_key and (by_key[failed_key].agent_type in sensitive
                                                or by_key[failed_key].requires_approval):
        why.append(f"the failed task '{failed_key}' was a sensitive step")
    return why


async def propose_or_apply(session: AsyncSession, wf: Workflow, patch: PlanPatch, *, created_by: str,
                           settings: Settings) -> PlanVersion:
    from app.services.workflow_service import enforce_sensitive_policy  # noqa: F401  (policy applied in apply())

    tasks = await _tasks(session, wf.id)
    rp = (wf.context or {}).get("replan") or {}
    version = PlanVersion(workflow_id=wf.id, version=await graph_service.next_plan_version(session, wf.id),
                          status="proposed", trigger=rp.get("trigger", "human"), reason=rp.get("reason"),
                          patch={**patch.model_dump(), "diff": patch_diff(patch)}, created_by=created_by)
    session.add(version)
    await session.flush()
    why = _needs_approval(wf, patch, tasks, settings)
    if why:
        now = utcnow()
        hours = settings.approval_timeout_hours
        approval = Approval(id=uuid.uuid4(), workflow_id=wf.id, task_id=None, kind="plan_patch", status="pending",
                            requested_at=now, expires_at=now + timedelta(hours=hours) if hours > 0 else None,
                            request_payload={"kind": "plan_patch", "version": version.version, "summary": patch.summary,
                                             "diff": patch_diff(patch), "why": why, "trigger": rp.get("trigger"),
                                             "workflow_goal": wf.goal})
        session.add(approval)
        version.approval_id = approval.id
        wf.context = {**wf.context, "replan": {**rp, "state": "proposed", "version": version.version}}
        await session.flush()
        record_event(session, workflow_id=wf.id, event_type=EventType.APPROVAL_REQUESTED,
                     payload={"approval_id": str(approval.id), "kind": "plan_patch", "version": version.version,
                              "why": why})
        return version
    await apply(session, wf, version, approved_by=None)
    return version


async def apply(session: AsyncSession, wf: Workflow, version: PlanVersion, *, approved_by: str | None) -> None:
    """Apply a validated patch in the caller's transaction (idempotent: an applied version is a no-op)."""
    from app.schemas.task import TaskCreate
    from app.services.workflow_service import build_tasks, enforce_sensitive_policy, record_task_created_events

    if version.status == "applied":
        return
    patch = PlanPatch.model_validate({k: v for k, v in version.patch.items() if k != "diff"})
    tasks = await _tasks(session, wf.id)
    by_key = {t.key: t for t in tasks}
    specs = [TaskCreate(key=t.key, name=t.name[:255], description=t.description, agent_type=t.agent_type,
                        input=t.input, requires_approval=t.requires_approval, depends_on=t.depends_on,
                        run_if=t.run_if, join=t.join, replan_if=t.replan_if) for t in patch.add_tasks]
    enforce_sensitive_policy(specs)
    new = build_tasks(wf.id, [s.model_copy(update={"depends_on": []}) for s in specs])  # linked below (existing too)
    # build_tasks links dependencies among the new tasks only; add links to existing tasks here
    new_by_key = {t.key: t for t in new}
    for spec in specs:
        new_by_key[spec.key].dependencies = [new_by_key.get(k) or by_key[k] for k in spec.depends_on]
    session.add_all(new)
    await session.flush()
    record_task_created_events(session, specs, new)
    for key in patch.cancel_tasks:
        await transition_task(session, by_key[key], TaskStatus.CANCELLED, event_type=EventType.TASK_CANCELLED,
                              reason=f"cancelled by re-plan v{version.version}")
    for rw in patch.rewire:
        old, repl = by_key[rw.from_failed], new_by_key[rw.to_replacement]
        await graph_service.rewire_dependents(session, tasks, old, repl, exclude={repl.id})
        await transition_task(session, old, TaskStatus.SUPERSEDED, reason=f"replaced by {repl.key}",
                              extra={"replacement": repl.key, "plan_version": version.version})
    version.status, version.approved_by, version.applied_at = "applied", approved_by, utcnow()
    rp = (wf.context or {}).get("replan") or {}
    adaptive = {**((wf.context or {}).get("adaptive") or {}), "replans": replans_done(wf) + 1}
    wf.context = {**wf.context, "adaptive": adaptive, "replan": {**rp, "state": "applied"}}
    record_event(session, workflow_id=wf.id, event_type=EventType.WORKFLOW_REPLAN_APPLIED,
                 payload={"version": version.version, "trigger": version.trigger, "diff": version.patch.get("diff"),
                          "added": [t.key for t in new], "cancelled": patch.cancel_tasks,
                          "superseded": [rw.from_failed for rw in patch.rewire], "approved_by": approved_by})
    if wf.status == WorkflowStatus.REPLANNING:
        back = rp.get("previous_status", "RUNNING")
        target = WorkflowStatus.PAUSED if back == "PAUSED" else WorkflowStatus.RUNNING
        if wf.error and back == "FAILED":
            wf.error = None
        await transition_workflow(session, wf, target, reason=f"re-plan v{version.version} applied")
    logger.info("workflow.replan_applied id=%s version=%d added=%s", wf.id, version.version, [t.key for t in new])


async def decide_patch(session: AsyncSession, wf: Workflow, approval: Approval, *, approved: bool, actor: str) -> None:
    version = await session.scalar(select(PlanVersion).where(PlanVersion.workflow_id == wf.id,
                                                             PlanVersion.approval_id == approval.id))
    if version is None:
        return
    if approved:
        await apply(session, wf, version, approved_by=actor)
        return
    version.status = "rejected"
    await _fail(session, wf, [f"the plan patch was rejected by {actor}"])


async def _fail(session: AsyncSession, wf: Workflow, errors: list[str]) -> None:
    """A re-plan that could not produce an acceptable patch: automatic -> FAILED; human -> back to before."""
    rp = (wf.context or {}).get("replan") or {}
    wf.context = {**wf.context, "replan": {**rp, "state": "failed", "errors": errors[:10]}}
    record_event(session, workflow_id=wf.id, event_type=EventType.WORKFLOW_REPLAN_FAILED,
                 payload={"trigger": rp.get("trigger"), "errors": errors[:10]})
    if wf.status != WorkflowStatus.REPLANNING:
        return
    back = rp.get("previous_status", "RUNNING")
    if rp.get("trigger") in AUTO_TRIGGERS or back == "FAILED":
        wf.error = wf.error or f"Re-planning failed: {'; '.join(errors[:3])}"
        await transition_workflow(session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
                                  reason=wf.error)
    else:
        await transition_workflow(session, wf, WorkflowStatus.PAUSED if back == "PAUSED" else WorkflowStatus.RUNNING,
                                  reason="re-plan failed; continuing with the current plan")


async def human_patch(session: AsyncSession, workflow_id: uuid.UUID, actor: str, patch: PlanPatch,
                      settings: Settings | None = None) -> PlanVersion:
    """POST /workflows/{id}/plan-patch: a human's own patch while PAUSED (same validation; actor recorded)."""
    from app.agents.registry import task_agent_types

    settings = settings or get_settings()
    wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if wf is None:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Workflow {workflow_id} not found")
    if wf.status != WorkflowStatus.PAUSED:
        raise ConflictError("A plan patch can only be submitted while the workflow is PAUSED")
    tasks = await _tasks(session, wf.id)
    errors = validate_patch(patch, tasks, task_agent_types(), max_new_tasks=settings.replan_max_new_tasks,
                            max_tasks=settings.max_plan_tasks + settings.replan_max_new_tasks * max(1, settings.max_replans),
                            max_loop_iterations=settings.max_loop_iterations)
    if errors:
        raise ValidationFailedError("; ".join(e.message for e in errors), details=[e.message for e in errors])
    wf.context = {**(wf.context or {}), "replan": {"state": "applying", "previous_status": "PAUSED",
                                                   "trigger": "human_patch", "actor": actor}}
    version = PlanVersion(workflow_id=wf.id, version=await graph_service.next_plan_version(session, wf.id),
                          status="proposed", trigger="human_patch", reason=patch.summary,
                          patch={**patch.model_dump(), "diff": patch_diff(patch)}, created_by=actor)
    session.add(version)
    await session.flush()
    await apply(session, wf, version, approved_by=actor)
    await session.commit()
    return version


async def _tasks(session: AsyncSession, workflow_id: uuid.UUID) -> list[Task]:
    return list(await session.scalars(select(Task).where(Task.workflow_id == workflow_id)
                                      .options(selectinload(Task.dependencies)).order_by(Task.created_at)
                                      .execution_options(populate_existing=True)))
