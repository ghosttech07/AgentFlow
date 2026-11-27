"""Scheduling steps. Each step is its own short DB transaction; the DB is the source of truth.

Per tick (see Engine.tick):
  pickup_run_requests  - start (or begin planning) workflows created with run=true
  schedule_workflow    - per RUNNING / AWAITING_APPROVAL workflow (never PAUSED): expire approvals /
                         fail / complete / promote / request approvals / RUNNING<->AWAITING_APPROVAL
  claim_tasks          - READY -> RUNNING with FOR UPDATE SKIP LOCKED + lease fields; a
                         requires_approval task only with an approved approval row
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.core.conditions import ConditionError, ConditionSyntaxError, parse_condition
from app.core.dag import TaskNode, leaves
from app.core.enums import TaskStatus, WorkflowStatus
from app.core.events import EventType, record_event
from app.core.joins import TOLERANT_JOINS, decide
from app.core.state_machine import transition_task, transition_workflow
from app.db.base import utcnow
from app.engine.retry import open_attempt, reclaim_lost_task
from app.models.approval import Approval
from app.models.task import Task
from app.models.workflow import Workflow
from app.services import approval_service, budget_service, planning_service
from app.services.notify import notify_approval
from app.services.workflow_service import start_workflow_locked

logger = logging.getLogger(__name__)

DONE_OK = frozenset({TaskStatus.SUCCEEDED, TaskStatus.SKIPPED, TaskStatus.SUPERSEDED})
# Workflows the scheduler works on. PAUSED is deliberately absent: nothing is promoted, gated or
# claimed while paused (the reaper still runs).
SCHEDULED = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL)


async def pickup_run_requests(
    session_factory: async_sessionmaker[AsyncSession], limit: int = 10
) -> tuple[int, list[uuid.UUID]]:
    """Workflows with run_requested=true and PENDING: start those with tasks, begin planning the rest.

    Rows are claimed with SKIP LOCKED, and planning moves them to PLANNING in the same
    transaction, so a workflow is never planned twice. Returns (started, ids_to_plan).
    """
    async with session_factory() as session:
        workflows = (
            await session.scalars(
                select(Workflow)
                .where(
                    Workflow.run_requested.is_(True),
                    # FAILED = planning failed and POST /retry asked for a re-plan
                    Workflow.status.in_([WorkflowStatus.PENDING, WorkflowStatus.FAILED]),
                )
                .order_by(Workflow.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        started, to_plan = 0, []
        for wf in workflows:
            task_count = await session.scalar(
                select(func.count()).select_from(Task).where(Task.workflow_id == wf.id)
            )
            if task_count and wf.status == WorkflowStatus.FAILED:
                wf.run_requested = False  # only unplanned FAILED workflows are re-planned here
            elif task_count:
                await start_workflow_locked(session, wf)
                started += 1
            else:
                await planning_service.begin_planning(
                    session, wf, planning_service.stored_constraints(wf)
                )
                to_plan.append(wf.id)
        await session.commit()
    return started, to_plan


async def schedule_running_workflows(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None
) -> int:
    """Run schedule_workflow for every RUNNING / AWAITING_APPROVAL workflow. Returns #changes."""
    async with session_factory() as session:
        ids = (
            await session.scalars(
                select(Workflow.id).where(Workflow.status.in_(SCHEDULED)).order_by(Workflow.started_at)
            )
        ).all()
    changes = 0
    for workflow_id in ids:
        async with session_factory() as session:
            changes += await schedule_workflow(session, workflow_id, settings)
    changes += await expire_replanning_approvals(session_factory)
    return changes


async def expire_replanning_approvals(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """A REPLANNING workflow is not scheduled, but its proposed plan_patch approval must still expire."""
    async with session_factory() as session:
        ids = (await session.scalars(select(Workflow.id).where(Workflow.status == WorkflowStatus.REPLANNING))).all()
    changes = 0
    for workflow_id in ids:
        async with session_factory() as session:
            wf = await session.scalar(select(Workflow).where(Workflow.id == workflow_id,
                                                             Workflow.status == WorkflowStatus.REPLANNING)
                                      .with_for_update(skip_locked=True))
            if wf is None or ((wf.context or {}).get("replan") or {}).get("state") != "proposed":
                await session.rollback()
                continue
            n = await approval_service.expire_due(session, wf, [], workflow_level_only=True)
            await session.commit()
            changes += n
    return changes


def can_progress(tasks: list[Task], now: datetime) -> bool:
    """True if something runs or can run now: READY/RUNNING, a due retry, a promotable PENDING task."""
    for t in tasks:
        if t.status in (TaskStatus.READY, TaskStatus.RUNNING):
            return True
        if t.status == TaskStatus.RETRYING and (t.next_attempt_at is None or t.next_attempt_at <= now):
            return True
        if t.status == TaskStatus.PENDING and decide(t)[0] != "wait":
            return True
    return False


async def schedule_workflow(
    session: AsyncSession, workflow_id: uuid.UUID, settings: Settings | None = None
) -> int:
    """One scheduling pass over a RUNNING / AWAITING_APPROVAL workflow, in one transaction."""
    settings = settings or get_settings()
    wf = await session.scalar(
        select(Workflow)
        .where(Workflow.id == workflow_id, Workflow.status.in_(SCHEDULED))
        .with_for_update(skip_locked=True)  # another engine (or a control request) holds it
    )
    if wf is None:
        await session.rollback()
        return 0
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.workflow_id == workflow_id)
                .options(selectinload(Task.dependencies))
                .order_by(Task.created_at, Task.key)
            )
        ).all()
    )

    # Expired approvals fail their task (approval_expired); the failure rule below fails the workflow.
    changes = await approval_service.expire_due(session, wf, tasks)
    if wf.status == WorkflowStatus.FAILED:  # a workflow-level approval expired
        await session.commit()
        return changes + 1

    # Feedback loops (Part 10): evaluate `until` / expand the next iteration BEFORE completion and promotion, so
    # nothing downstream of an open loop runs early and a finished first iteration never completes the workflow.
    if (wf.context or {}).get("loops"):
        loop_changes = await _advance_loops(session, wf, tasks, settings)
        if wf.status == WorkflowStatus.FAILED:
            await session.commit()
            return changes + 1
        if loop_changes:
            changes += loop_changes
            tasks = list((await session.scalars(
                select(Task).where(Task.workflow_id == workflow_id).options(selectinload(Task.dependencies))
                .order_by(Task.created_at, Task.key).execution_options(populate_existing=True))).all())

    # Failure: stop scheduling, but leave PENDING tasks untouched (Part 5 resumes them)
    # and let running siblings finish and save their output.
    # Re-plan trigger (b): a succeeded task whose replan_if is true (evaluated once per task).
    if await _check_replan_if(session, wf, tasks, settings):
        await session.commit()
        return changes + 1

    failed = [t for t in tasks if t.status == TaskStatus.FAILED and not tolerated_failure(t, tasks)]
    # Re-plan trigger (a): the workflow opted in (planner-made plans) and a task failed for good.
    if failed and _replan_on_failure(wf, failed[0], settings):
        from app.services import replan_service

        await replan_service.request(session, wf, trigger="task_failed", failed_task=failed[0].key,
                                     reason=f"task '{failed[0].key}' failed ({failed[0].error_type}): {failed[0].error}")
        await session.commit()
        return changes + 1
    if failed:
        first = failed[0]
        wf.error = f"Task '{first.key}' failed ({first.error_type or 'unknown'}): {first.error}"
        await transition_workflow(
            session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
            reason=wf.error, extra={"failed_tasks": [t.key for t in failed]},
        )
        await session.commit()
        logger.warning("workflow.failed id=%s failed_tasks=%s", wf.id, [t.key for t in failed])
        return changes + 1

    if tasks and not _loops_open(wf) and all(
            t.status in DONE_OK or (t.status == TaskStatus.FAILED and tolerated_failure(t, tasks)) for t in tasks):
        if wf.status == WorkflowStatus.AWAITING_APPROVAL:  # not reachable normally; keep it valid anyway
            await transition_workflow(session, wf, WorkflowStatus.RUNNING)
        await _complete(session, wf, tasks)
        await session.commit()
        return changes + 1

    # Budget (Part 8): a reached limit pauses the workflow here, after the failure and
    # completion rules (a finished workflow is never paused) and before anything is promoted or claimed (claim_tasks only takes READY tasks of RUNNING workflows).
    eff = budget_service.effective_budget(wf.budget, settings)
    usage = await budget_service.get_usage(session, wf)
    if (limit := budget_service.exceeded(eff, usage)) is not None:
        await budget_service.pause_locked(session, wf, limit, usage, eff)
        await session.commit()
        return 1

    now = utcnow()
    for t in tasks:
        # Promote PENDING tasks per their join rule (Part 10; default: all dependencies succeeded), then run_if.
        if t.status == TaskStatus.PENDING and not _blocked_by_open_loop(wf, t):
            changes += await _promote(session, t)
        # Retry: a RETRYING task whose backoff has elapsed becomes READY again.
        elif t.status == TaskStatus.RETRYING and (t.next_attempt_at is None or t.next_attempt_at <= now):
            t.next_attempt_at = None
            await transition_task(session, t, TaskStatus.READY, event_type=EventType.TASK_READY,
                                  extra={"retry": True, "next_attempt": t.attempt_count + 1})
            changes += 1

    # Approval gate: a READY task that needs approval and has none approved waits for a human.
    # An approval stays valid for automatic retries / crash recovery of the same task.
    requested: list[Approval] = []
    # Part 11: a policy rule can require an approval for a task (it can only add one, never remove it).
    from app.governance.policy import get_policies

    policies = get_policies()
    if policies.task_rules():
        team = await approval_service.team_name(session, wf)
        for t in tasks:
            if t.status == TaskStatus.READY and not t.requires_approval and policies.evaluate(
                    scope="task", agent_type=t.agent_type, task_key=t.base_key or t.key, team=team).require:
                t.requires_approval = True
    gated = [t for t in tasks if t.status == TaskStatus.READY and t.requires_approval]
    if gated:
        approved = await approval_service.approved_task_ids(session, wf.id)
        for t in gated:
            if t.id not in approved:
                requested.append(await approval_service.request_approval(session, wf, t, settings))
                changes += 1

    # Independent branches keep running; the workflow is AWAITING_APPROVAL only while nothing
    # can run and at least one approval is pending.
    waiting = any(t.status == TaskStatus.AWAITING_APPROVAL for t in tasks) or _loops_open(wf, "awaiting_human")
    if wf.status == WorkflowStatus.RUNNING and waiting and not can_progress(tasks, now):
        await transition_workflow(
            session, wf, WorkflowStatus.AWAITING_APPROVAL, event_type=EventType.WORKFLOW_AWAITING_APPROVAL,
            extra={"tasks": [t.key for t in tasks if t.status == TaskStatus.AWAITING_APPROVAL]},
        )
        changes += 1
        logger.info("workflow.awaiting_approval id=%s", wf.id)
    elif wf.status == WorkflowStatus.AWAITING_APPROVAL and (not waiting or can_progress(tasks, now)):
        await transition_workflow(session, wf, WorkflowStatus.RUNNING, reason="work can run again")
        changes += 1

    await session.commit()
    for a in requested:  # after commit: the approval exists before anyone is told about it
        notify_approval(settings.approval_webhook_url, {"approval_id": str(a.id), **a.request_payload})
    return changes


OPEN_LOOP = ("open", "awaiting_human", "expand_pending", "choose_best")
BODY_DEAD = (TaskStatus.SKIPPED, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED)


def _finished_iterations(tasks: list[Task], spec: Any, k: int) -> list[int]:
    """Iterations 1..k whose whole body SUCCEEDED (only those can be chosen as the result)."""
    from app.services import graph_service

    done = []
    for i in range(1, k + 1):
        body = graph_service.iteration_tasks(tasks, spec.name, i)
        if len(body) == len(spec.body) and all(t.status == TaskStatus.SUCCEEDED for t in body.values()):
            done.append(i)
    return done


def _loops_open(wf: Workflow, *statuses: str) -> bool:
    wanted = statuses or OPEN_LOOP
    return any(s.get("status") in wanted for s in (wf.context or {}).get("loops", {}).values())


def _blocked_by_open_loop(wf: Workflow, t: Task) -> bool:
    """A task downstream of a loop waits until that loop has decided (ruling 5); body tasks are not blocked."""
    loops = (wf.context or {}).get("loops", {})
    return any(d.loop_name and d.loop_name != t.loop_name and loops.get(d.loop_name, {}).get("status") in OPEN_LOOP
               for d in t.dependencies)


async def _advance_loops(session: AsyncSession, wf: Workflow, tasks: list[Task], settings: Settings) -> int:
    from app.core.adaptive_spec import LoopSpec
    from app.services import graph_service

    changes = 0
    for name, state in list((wf.context or {}).get("loops", {}).items()):
        spec = LoopSpec.model_validate(state["spec"])
        k = int(state.get("iteration", 1))
        status = state.get("status")
        if status == "expand_pending":
            await graph_service.expand_loop(session, wf, tasks, name, k + 1)
            _set_loop(wf, name, status="open")
            changes += 1
            continue
        if status == "choose_best":
            await _choose_best(session, wf, tasks, spec, k, state.get("reason", "approved by a human"))
            changes += 1
            continue
        if status != "open":
            continue
        current = graph_service.iteration_tasks(tasks, name, k)
        dead = [t for t in current.values() if t.status in BODY_DEAD]
        if dead:  # a skipped / cancelled / superseded body task can never finish this iteration: decide now
            await _exhausted(session, wf, tasks, spec, k, "loop body did not finish: " + ", ".join(
                f"{t.key} {t.status}" for t in dead))
            changes += 1
            continue
        last = current.get(spec.body[-1])
        if last is None or last.status != TaskStatus.SUCCEEDED or len(current) != len(spec.body):
            continue
        outputs = graph_service.iteration_outputs(tasks, name, k)
        try:
            done, values = parse_condition(spec.until).evaluate(outputs)
            reason = None
        except (ConditionError, ConditionSyntaxError) as exc:
            done, values, reason = False, {}, f"condition_error: until could not be evaluated ({exc})"
        record_event(session, workflow_id=wf.id, event_type=EventType.LOOP_CONDITION_EVALUATED,
                     payload={"loop": name, "iteration": k, "expression": spec.until, "result": done, "values": values,
                              **({"error": reason} if reason else {})})
        changes += 1
        if done:
            if k > 1:
                await _lesson(session, wf, tasks, spec, k)
            _set_loop(wf, name, status="done", chosen_iteration=k)
            record_event(session, workflow_id=wf.id, event_type=EventType.LOOP_COMPLETED,
                         payload={"loop": name, "iteration": k})
            continue
        if reason is None:
            reason = await _iteration_blocker(session, wf, tasks, spec, k, settings)
        if reason is None:
            await graph_service.expand_loop(session, wf, tasks, name, k + 1)
            continue
        await _exhausted(session, wf, tasks, spec, k, reason)
    return changes


async def _iteration_blocker(session: AsyncSession, wf: Workflow, tasks: list[Task], spec: Any, k: int,
                             settings: Settings) -> str | None:
    """Why another iteration may not start (None = it may)."""
    state = wf.context["loops"][spec.name]
    if k >= spec.max_iterations + int(state.get("extra_iterations", 0)):
        return f"max_iterations reached ({k})"
    eff = budget_service.effective_budget(wf.budget, settings)
    extra_done = sum(int(s.get("iteration", 1)) - 1 for s in wf.context["loops"].values())
    if eff.get("max_iterations_total") is not None and extra_done >= eff["max_iterations_total"]:
        return f"budget: max_iterations_total={eff['max_iterations_total']} reached"
    if eff.get("max_llm_calls") is not None:
        from app.models.usage import LLMCall

        ids = [t.id for t in tasks if t.loop_name == spec.name and (t.iteration or 1) == k]
        per_iteration = await session.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.task_id.in_(ids)))
        used = (await budget_service.get_usage(session, wf)).llm_calls
        if used + int(per_iteration or 0) > eff["max_llm_calls"]:
            return f"budget: another iteration (~{per_iteration} LLM calls) would cross max_llm_calls={eff['max_llm_calls']}"
    return None


async def _lesson(session: AsyncSession, wf: Workflow, tasks: list[Task], spec: Any, k: int) -> None:
    from app.services import graph_service, memory_service

    reviews = []
    for i in range(1, k + 1):
        reviewer = graph_service.iteration_tasks(tasks, spec.name, i).get(spec.feedback.from_)
        reviews.append((reviewer.output if reviewer else None) or {})
    body = [t for t in tasks if t.loop_name == spec.name]
    await memory_service.write_lesson(session, wf, reviews, tainted=any(t.tainted for t in body),
                                      private=any(memory_service.is_private(t) for t in body))


def _set_loop(wf: Workflow, name: str, **values: Any) -> None:
    loops = dict(wf.context.get("loops", {}))
    loops[name] = {**loops[name], **values}
    wf.context = {**wf.context, "loops": loops}


async def _exhausted(session: AsyncSession, wf: Workflow, tasks: list[Task], spec: Any, k: int, reason: str) -> None:
    mode = spec.on_exhausted
    if mode == "fail":
        _set_loop(wf, spec.name, status="exhausted", reason=reason)
        record_event(session, workflow_id=wf.id, event_type=EventType.LOOP_EXHAUSTED,
                     payload={"loop": spec.name, "iteration": k, "mode": mode, "reason": reason})
        wf.error = f"Loop '{spec.name}' ended without meeting its condition ({reason})"
        await transition_workflow(session, wf, WorkflowStatus.FAILED, event_type=EventType.WORKFLOW_FAILED,
                                  reason=wf.error)
        return
    if mode == "ask_human":
        _set_loop(wf, spec.name, status="awaiting_human", reason=reason)
        await approval_service.request_loop_approval(session, wf, spec, k, reason, tasks)
        record_event(session, workflow_id=wf.id, event_type=EventType.LOOP_EXHAUSTED,
                     payload={"loop": spec.name, "iteration": k, "mode": mode, "reason": reason})
        return
    await _choose_best(session, wf, tasks, spec, k, reason)


async def _choose_best(session: AsyncSession, wf: Workflow, tasks: list[Task], spec: Any, k: int, reason: str) -> None:
    from app.services import graph_service

    expr = spec.best_by or f"{spec.feedback.from_}.output.score"
    scores: dict[int, Any] = {}
    finished = _finished_iterations(tasks, spec, k)
    latest_finished = finished[-1] if finished else k
    try:
        cond = parse_condition(expr)
        for i in finished:
            scores[i] = cond.value(graph_service.iteration_outputs(tasks, spec.name, i))
        chosen = max(scores, key=lambda i: (scores[i], i)) if scores else latest_finished
    except (ConditionError, ConditionSyntaxError, TypeError) as exc:
        chosen = latest_finished
        reason = f"{reason}; best_by could not be evaluated ({exc}), using the latest finished iteration"
    if chosen != k:
        latest = graph_service.iteration_tasks(tasks, spec.name, k)
        best = graph_service.iteration_tasks(tasks, spec.name, chosen)
        body_ids = {t.id for t in tasks if t.loop_name == spec.name}
        for base in spec.body:  # everything downstream uses the chosen draft
            await graph_service.rewire_dependents(session, tasks, latest[base], best[base], exclude=body_ids)
    _set_loop(wf, spec.name, status="done", chosen_iteration=chosen, reason=reason)
    record_event(session, workflow_id=wf.id, event_type=EventType.LOOP_EXHAUSTED,
                 payload={"loop": spec.name, "iteration": k, "mode": "continue_with_best", "chosen_iteration": chosen,
                          "scores": {str(i): v for i, v in scores.items()}, "reason": reason})


def _replan_on_failure(wf: Workflow, failed: Task, settings: Settings) -> bool:
    from app.services import replan_service

    opted_in = bool(((wf.context or {}).get("adaptive") or {}).get("replan_on_failure"))
    # condition_error never triggers a re-plan by itself (ruling 6); a human rejection is a decision, not a failure.
    return (opted_in and failed.error_type not in ("condition_error", "rejected")
            and replan_service.can_replan(wf, settings))


async def _check_replan_if(session: AsyncSession, wf: Workflow, tasks: list[Task], settings: Settings) -> bool:
    from app.services import replan_service

    adaptive = dict((wf.context or {}).get("adaptive") or {})
    checked = set(adaptive.get("replan_if_checked", []))
    for t in tasks:
        if not t.replan_if or t.status != TaskStatus.SUCCEEDED or str(t.id) in checked:
            continue
        checked.add(str(t.id))
        wf.context = {**wf.context, "adaptive": {**adaptive, "replan_if_checked": sorted(checked)}}
        try:
            ok, values = parse_condition(t.replan_if).evaluate(condition_outputs([t, *t.dependencies]))
            payload = {"task_key": t.key, "expression": t.replan_if, "result": ok, "values": values, "kind": "replan_if"}
        except (ConditionError, ConditionSyntaxError) as exc:
            ok, payload = False, {"task_key": t.key, "expression": t.replan_if, "error": str(exc), "kind": "replan_if"}
        record_event(session, workflow_id=wf.id, task_id=t.id, event_type=EventType.TASK_CONDITION_EVALUATED,
                     payload=payload)
        if ok and replan_service.can_replan(wf, settings):
            await replan_service.request(session, wf, trigger="replan_if", failed_task=None,
                                         reason=f"replan_if on '{t.key}' is true ({t.replan_if})")
            return True
    return False


def tolerated_failure(task: Task, tasks: list[Task]) -> bool:
    """A failed task is tolerated when every task that depends on it accepts a failed dependency (Part 10)."""
    dependents = [t for t in tasks if any(d.id == task.id for d in t.dependencies)]
    return bool(dependents) and all((t.join or "all_succeeded") in TOLERANT_JOINS for t in dependents)


def condition_outputs(deps: list[Task]) -> dict[str, dict[str, Any]]:
    """Outputs by key; a loop iteration also answers to its base key (review_guide -> review_guide__v2)."""
    out = {d.key: {"output": d.output, "status": str(d.status)} for d in deps}
    for d in deps:
        if d.base_key and d.base_key not in out:
            out[d.base_key] = out[d.key]
    return out


async def _promote(session: AsyncSession, t: Task) -> int:
    decision, reason = decide(t)
    if decision == "wait":
        return 0
    if decision == "skip":
        t.skip_reason = reason
        await transition_task(session, t, TaskStatus.SKIPPED, event_type=EventType.TASK_SKIPPED, reason=reason)
        return 1
    if t.run_if:
        try:
            ok, values = parse_condition(t.run_if).evaluate(condition_outputs(list(t.dependencies)))
        except (ConditionError, ConditionSyntaxError) as exc:
            record_event(session, workflow_id=t.workflow_id, task_id=t.id, event_type=EventType.TASK_CONDITION_EVALUATED,
                         payload={"task_key": t.key, "expression": t.run_if, "error": str(exc)})
            t.error_type, t.error = "condition_error", f"run_if could not be evaluated: {exc}"
            await transition_task(session, t, TaskStatus.FAILED, event_type=EventType.TASK_FAILED,
                                  reason="condition_error")
            return 1
        record_event(session, workflow_id=t.workflow_id, task_id=t.id, event_type=EventType.TASK_CONDITION_EVALUATED,
                     payload={"task_key": t.key, "expression": t.run_if, "result": ok, "values": values})
        if not ok:
            t.skip_reason = "condition_false"
            await transition_task(session, t, TaskStatus.SKIPPED, event_type=EventType.TASK_SKIPPED,
                                  reason="condition_false")
            return 1
    await transition_task(session, t, TaskStatus.READY, event_type=EventType.TASK_READY)
    return 1


def _chosen(wf: Workflow, t: Task) -> bool:
    """Loop body tasks count only from the chosen (final) iteration; earlier drafts stay in the history."""
    if not t.loop_name:
        return True
    state = (wf.context or {}).get("loops", {}).get(t.loop_name, {})
    return (t.iteration or 1) == (state.get("chosen_iteration") or state.get("iteration", 1))


def _chosen_version(wf: Workflow, tasks: list[Task], t: Task) -> Task | None:
    """A leaf from a loop body stands for its loop: report the CHOSEN iteration's task with the same base key
    (continue_with_best may pick an earlier draft at the end of the plan)."""
    if _chosen(wf, t):
        return t
    from app.services import graph_service

    state = (wf.context or {}).get("loops", {}).get(t.loop_name, {})
    chosen = state.get("chosen_iteration")
    if not chosen:
        return None
    return graph_service.iteration_tasks(tasks, t.loop_name, int(chosen)).get(t.base_key or t.key)


async def _complete(session: AsyncSession, wf: Workflow, tasks: list[Task]) -> None:
    nodes = [TaskNode(key=t.key, agent_type=t.agent_type, depends_on=[d.key for d in t.dependencies])
             for t in tasks]
    by_key = {t.key: t for t in tasks}
    tokens: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    for t in tasks:
        for k in tokens:
            tokens[k] += int((t.metrics or {}).get(k) or 0)
    failed_branches = [{"key": t.key, "error_type": t.error_type, "error": t.error}
                       for t in tasks if t.status == TaskStatus.FAILED]  # only tolerated ones reach completion
    final: dict[str, Any] = {}
    for key in leaves(nodes):
        t = _chosen_version(wf, tasks, by_key[key])
        if t is not None and t.status == TaskStatus.SUCCEEDED:
            final[t.key] = t.output
    wf.result = {
        "final_outputs": final,
        "summary": (wf.context or {}).get("plan_summary"),
        "skipped": [t.key for t in tasks if t.status == TaskStatus.SKIPPED],
        **({"failed_branches": failed_branches} if failed_branches else {}),
    }
    from app.services import memory_service

    await memory_service.write_artifact_summary(session, wf)
    if failed_branches:  # completed, but must not look clean
        record_event(session, workflow_id=wf.id, event_type=EventType.WORKFLOW_COMPLETED_WITH_FAILED_BRANCHES,
                     payload={"failed_branches": failed_branches})
    duration = (utcnow() - wf.started_at).total_seconds() if wf.started_at else None
    await transition_workflow(
        session, wf, WorkflowStatus.COMPLETED, event_type=EventType.WORKFLOW_COMPLETED,
        extra={"duration_seconds": round(duration, 2) if duration is not None else None,
               "task_count": len(tasks), **tokens},
    )
    logger.info("workflow.completed id=%s tasks=%d duration_s=%.1f tokens=%s",
                wf.id, len(tasks), duration or 0, tokens)


class Claim(NamedTuple):
    """What a worker needs to own a task: the id and the attempt it was claimed for."""

    task_id: uuid.UUID
    attempt: int


async def claim_tasks(
    session: AsyncSession, worker_id: str, limit: int, lease_seconds: float
) -> list[Claim]:
    """Claim up to `limit` READY tasks of RUNNING workflows, atomically.

    FOR UPDATE SKIP LOCKED: concurrent claimers (other coroutines or engine processes) skip rows
    another transaction holds, so a task is never claimed twice. The attempt history row is
    inserted in the same transaction.
    """
    if limit <= 0:
        return []
    tasks = (
        await session.scalars(
            select(Task)
            .join(Workflow, Task.workflow_id == Workflow.id)
            .where(
                Task.status == TaskStatus.READY,
                Workflow.status == WorkflowStatus.RUNNING,
                # Claim-time double check: never trust the task status alone for gated tasks.
                or_(Task.requires_approval.is_(False),
                    exists().where(Approval.task_id == Task.id, Approval.status == "approved",
                                   Approval.kind == "task")),
            )
            .order_by(Workflow.started_at, Task.created_at, Task.key)
            .limit(limit)
            .with_for_update(of=Task, skip_locked=True)
        )
    ).all()
    expires = utcnow() + timedelta(seconds=lease_seconds)
    claims: list[Claim] = []
    for t in tasks:
        t.attempt_count += 1
        t.lease_owner = worker_id
        t.lease_expires_at = expires
        t.last_heartbeat_at = None
        t.next_attempt_at = None
        row = await open_attempt(session, t, worker_id)
        await transition_task(
            session, t, TaskStatus.RUNNING, event_type=EventType.TASK_STARTED,
            extra={"attempt": t.attempt_count, "attempt_number": row.attempt_number,
                   "worker_id": worker_id},
        )
        claims.append(Claim(t.id, t.attempt_count))
    await session.commit()
    return claims


async def reap_expired_leases(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """RUNNING tasks whose lease expired (worker died or hung): worker_lost -> retry policy."""
    async with session_factory() as session:
        tasks = (
            await session.scalars(
                select(Task)
                .where(Task.status == TaskStatus.RUNNING, Task.lease_expires_at < utcnow())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for t in tasks:
            logger.warning("task.lease_expired key=%s owner=%s attempt=%d", t.key, t.lease_owner, t.attempt_count)
            await reclaim_lost_task(session, t, settings, reason="lease expired")
        await session.commit()
    return len(tasks)


async def next_retry_due(session_factory: async_sessionmaker[AsyncSession]) -> datetime | None:
    """Earliest next_attempt_at of a RETRYING task in a scheduled workflow (engine wake-up time)."""
    async with session_factory() as session:
        return await session.scalar(
            select(func.min(Task.next_attempt_at))
            .join(Workflow, Task.workflow_id == Workflow.id)
            .where(Task.status == TaskStatus.RETRYING, Workflow.status.in_(SCHEDULED))
        )


def task_metrics(result: Any) -> dict[str, Any]:
    """Metrics stored on a task from an AgentResult."""
    return {
        "provider": getattr(result, "provider", None),
        "cost_usd": getattr(result, "cost_usd", None),
        "fallback_from": getattr(result, "fallback_from", None),
        "route_position": getattr(result, "route_position", 0),
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "sources": [s.model_dump() for s in result.sources],
        **({"tools": result.tool_summary} if getattr(result, "tool_summary", None) else {}),
    }
