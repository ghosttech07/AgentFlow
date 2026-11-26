"""The provider-agnostic tool loop (Part 9).

Each step the model returns ONE structured action through the normal router (cost, quota, budget and
fallback apply to every step): a tool call or the final output. Code validates the call (allowed tool?
arguments valid?), consults the journal (reuse / unknown outcome), decides whether a human must approve,
executes with a timeout and journals the result. Invalid calls are returned to the model as tool errors.

Resume: a new attempt after an attempt that did not fail by itself (worker lost, interrupted, budget
pause, suspended for approval) rebuilds the transcript from the journal and asks the model only for the
NEXT step, so a crash never makes the model re-propose (and re-send) an earlier call.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import utcnow
from app.llm.errors import BudgetPause, LLMOutputError
from app.models.approval import Approval
from app.models.tool_call import ToolCall
from app.tools import journal
from app.tools.base import WRITE_EFFECTS, Tool, ToolContext, ToolError
from app.tools.config import CredentialResolver, ToolsConfig, approval_mode
from app.tools.exfil import ExfilContext, check_read_url, tokens_in, urls_in
from app.tools.prompting import FINAL_NOW, PROTOCOL, UNTRUSTED_RULE, StepView, steps_section, tools_section
from app.tools.registry import get_tool
from app.tools.sources import normalize_url

if TYPE_CHECKING:
    from app.agents.base import AgentInput, BaseAgent

logger = logging.getLogger(__name__)


class LoopState(BaseModel):
    """Checkpoint saved on a suspended attempt (task_attempts.loop_state)."""

    step_index: int = 0
    tainted: bool = False
    untrusted_sources: list[str] = []
    injection_warnings: list[str] = []
    pending_call_id: uuid.UUID | None = None


@dataclass
class ResumeInfo:
    attempt_numbers: list[int]  # earlier attempts whose journal rows form the transcript
    state: LoopState | None = None


@dataclass
class ToolRuntime:
    session_factory: async_sessionmaker[AsyncSession]
    config: ToolsConfig
    settings: Settings
    workflow_id: uuid.UUID
    task_id: uuid.UUID
    task_key: str
    attempt_number: int  # task_attempts.attempt_number of the running attempt
    tools: list[str]
    tainted_from: list[str] = field(default_factory=list)  # upstream tainted task keys
    # What made the upstream tainted (URLs read, injection flags), so an approver of a later send sees it.
    inherited_sources: list[str] = field(default_factory=list)
    inherited_warnings: list[str] = field(default_factory=list)
    private_context: bool = False  # the task starts holding private data (e.g. a private memory; Part 10)
    resume: ResumeInfo | None = None


class ToolApprovalNeeded(Exception):  # noqa: N818  (a control signal, like BudgetPause)
    """A proposed call needs a human: the worker suspends the task (no retry consumed)."""

    def __init__(self, tool_call_id: uuid.UUID, payload: dict[str, Any], state: LoopState) -> None:
        super().__init__(f"tool call {tool_call_id} needs approval")
        self.tool_call_id = tool_call_id
        self.payload = payload
        self.state = state


@dataclass
class LoopReport:
    steps: int = 0
    tool_calls: int = 0
    invalid_steps: int = 0
    reused: int = 0
    tainted: bool = False
    untrusted_sources: list[str] = field(default_factory=list)
    injection_warnings: list[str] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    by_tool: dict[str, int] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = 0.0
    fallback_from: str | None = None
    route_position: int = 0

    sources_verified: dict[str, Any] | None = None  # set by the researcher's code check
    calls: list[dict[str, Any]] = field(default_factory=list)  # every step (tool, status, arguments, result, ...)

    def summary(self) -> dict[str, Any]:
        out = {"steps": self.steps, "tool_calls": self.tool_calls, "invalid_steps": self.invalid_steps,
               "reused": self.reused, "tainted": self.tainted, "by_tool": self.by_tool,
               "untrusted_sources": self.untrusted_sources[:20], "injection_warnings": self.injection_warnings}
        if self.sources_verified is not None:
            out["sources_verified"] = self.sources_verified
        return out


PRIVATE_TOOLS = frozenset({"sql_query", "file_read"})  # results that make an attempt hold private data
READ_URL_TOOLS = frozenset({"web_fetch", "http_request"})


def build_step_model(output_model: type[BaseModel], tool_names: list[str]) -> type[BaseModel]:
    """Flat, strict-schema friendly step: every key present, unused ones null. `arguments` is a free object
    (validated by code against the named tool's input_model, so a bad call is a tool error, not a crash)."""
    model = create_model(
        f"ToolStep{output_model.__name__}",
        action=(Literal["tool_call", "final"], ...),
        tool=(str | None, None),
        arguments=(dict[str, Any] | None, None),
        reason=(str | None, None),
        output=(output_model | None, None),
    )
    model.__agent_output__ = output_model  # type: ignore[attr-defined]  # FakeLLMClient uses it
    model.__tool_names__ = tool_names  # type: ignore[attr-defined]
    return model


def _source_label(tool: str, args: dict[str, Any] | None) -> str:
    args = args or {}
    return str(args.get("url") or (f"search: {args['query']}" if "query" in args else tool))[:300]


def _urls_from(row_tool: str, args: dict[str, Any] | None, result: dict[str, Any] | None) -> set[str]:
    urls: set[str] = set()
    if not result:
        return urls
    if row_tool == "web_search":
        urls |= {h["url"] for h in result.get("results", []) if h.get("url")}
    elif row_tool == "web_fetch":
        urls |= {u for u in (result.get("url"), result.get("final_url")) if u}
    return urls


class ToolLoop:
    def __init__(self, agent: BaseAgent, agent_input: AgentInput, rt: ToolRuntime) -> None:
        self.agent, self.input, self.rt = agent, agent_input, rt
        self.sf = rt.session_factory
        self.tools: dict[str, Tool] = {n: get_tool(n) for n in rt.tools}
        self.history: list[StepView] = []
        self.report = LoopReport()
        self.state = LoopState(tainted=bool(rt.tainted_from),
                               untrusted_sources=[*[f"upstream task {k}" for k in rt.tainted_from],
                                                  *rt.inherited_sources],
                               injection_warnings=list(rt.inherited_warnings))
        self.credentials = CredentialResolver(rt.config)
        # Exfiltration guard (Part 10): what this attempt has seen as data, and whether it holds private data.
        self.private = rt.private_context
        self.private_tokens: set[str] = set()
        self.provenance: set[str] = urls_in(agent_input.task_input) | urls_in(agent_input.dependency_outputs)

    # ------------------------------------------------------------------ helpers
    def _options(self, tool: Tool) -> dict[str, Any]:
        entry = self.rt.config.entry(tool.name)
        return entry.options() if entry else {}

    def _ctx(self, tool: Tool, key: str, flags: dict[str, Any] | None = None) -> ToolContext:
        return ToolContext(workflow_id=self.rt.workflow_id, task_key=self.rt.task_key,
                           attempt=self.rt.attempt_number,
                           sandbox=self.rt.settings.workspaces_dir / str(self.rt.workflow_id),
                           idempotency_key=key, settings=self.rt.settings, options=self._options(tool),
                           credentials=self.credentials, approval_flags=flags or {})

    def _view(self, row: ToolCall) -> StepView:
        tool = self.tools.get(row.tool)
        untrusted = bool(tool and tool.untrusted)
        return StepView(index=row.step_index, tool=row.tool, arguments=row.arguments, status=row.status,
                        result=row.result, error=row.error, untrusted=untrusted,
                        source=_source_label(row.tool, row.arguments), reused=row.reused_from_id is not None,
                        call_id=row.id)

    def _absorb(self, view: StepView) -> None:
        """Account for a step in the report and the taint state."""
        self.history.append(view)
        self.report.by_tool[view.tool] = self.report.by_tool.get(view.tool, 0) + 1
        if view.status == "rejected_invalid":
            self.report.invalid_steps += 1
        if view.status == "succeeded":
            if view.tool in PRIVATE_TOOLS:
                self.private = True
                self.private_tokens |= tokens_in(view.result or {})
            self.provenance |= {normalize_url(u) for u in _urls_from(view.tool, view.arguments, view.result)}
            self.report.tool_calls += 1
            self.report.reused += int(view.reused)
            self.report.seen_urls |= _urls_from(view.tool, view.arguments, view.result)
            if view.untrusted:
                self.state.tainted = True
                if view.source not in self.state.untrusted_sources:
                    self.state.untrusted_sources.append(view.source)

    def _base_row(self, step_index: int, tool: str, side_effect: str, **extra: Any) -> dict[str, Any]:
        return {"workflow_id": self.rt.workflow_id, "task_id": self.rt.task_id,
                "attempt_number": self.rt.attempt_number, "step_index": step_index, "tool": tool,
                "side_effect": side_effect, "tainted_context": self.state.tainted, **extra}

    # ------------------------------------------------------------------ resume
    async def _restore(self) -> None:
        resume = self.rt.resume
        if resume is None:
            return
        if resume.state is not None:
            self.state = resume.state.model_copy(deep=True)
        rows = await journal.transcript(self.sf, self.rt.task_id, resume.attempt_numbers)
        for row in rows:
            if row.status in ("awaiting_approval", "approved", "running", "unknown_outcome", "proposed", "superseded"):
                continue  # handled below (pending / unknown calls); superseded rows were replaced by a later row
            self._absorb(self._view(row))
        self.state.step_index = max([r.step_index + 1 for r in rows], default=0)
        # Calls whose outcome is open: the approved/denied pending call, or a call a dead attempt was running.
        for row in rows:
            if row.status in ("approved", "denied") and row.id == self.state.pending_call_id:
                if row.status == "approved":
                    await self._execute_approved(row)
                continue
            if row.status in ("running", "unknown_outcome"):
                await self._settle_unknown(row)
        self.state.pending_call_id = None

    async def _execute_approved(self, row: ToolCall) -> None:
        tool = self.tools.get(row.tool) or get_tool(row.tool)
        flags: dict[str, Any] = {}
        if row.approval_id is not None:
            async with self.sf() as s:
                approval = await s.get(Approval, row.approval_id)
                flags = dict(((approval.input_overrides or {}).get("flags") or {}) if approval else {})
        args = tool.input_model.model_validate(row.arguments or {})
        await journal.set_status(self.sf, row.id, "running", started_at=utcnow(), attempt_number=self.rt.attempt_number)
        view = await self._run(tool, args, row.id, row.idempotency_key or "", row.step_index, flags)
        self._absorb(view)

    async def _settle_unknown(self, row: ToolCall) -> None:
        """A dead attempt was running this call: apply the unknown-outcome rules."""
        tool = self.tools.get(row.tool) or get_tool(row.tool)
        # Close it for good: the new row (re-run or human decision) replaces it, so it is never settled twice.
        await journal.set_status(self.sf, row.id, "superseded", finished_at=utcnow(),
                                 error="outcome unknown (its attempt ended while it ran); replaced by a new call")
        args = tool.input_model.model_validate(row.arguments or {})
        decision = journal.reuse_decision(ToolCall(status="unknown_outcome"), row.side_effect,
                                          tool.replay_safe(self._options(tool)), utcnow(), 0)
        if decision == "unknown_needs_approval":
            await self._suspend(tool, args, row.reason, row.step_index, row.idempotency_key or "",
                                warning="A previous attempt may have sent this (its outcome is unknown). "
                                        "Approve only if it did not arrive.")
        call_id = await journal.record_start(self.sf, secrets=self.credentials.secret_values(),
                                             **self._base_row(row.step_index, row.tool, row.side_effect,
                                                              arguments=row.arguments, args_hash=row.args_hash,
                                                              idempotency_key=row.idempotency_key, reason=row.reason))
        self._absorb(await self._run(tool, args, call_id, row.idempotency_key or "", row.step_index))

    # ------------------------------------------------------------------ one call
    async def _run(self, tool: Tool, args: BaseModel, call_id: uuid.UUID, key: str, step_index: int,
                   flags: dict[str, Any] | None = None) -> StepView:
        start = time.monotonic()
        status, result, error = "succeeded", None, None
        is_write = tool.side_effect_for(args) in WRITE_EFFECTS
        try:
            out = await asyncio.wait_for(tool.execute(args, self._ctx(tool, key, flags)), timeout=tool.timeout_seconds)
            result = out.model_dump(mode="json")
        except asyncio.CancelledError:
            # Cancel / immediate pause / lost lease while the tool ran: it may have happened. Journal that, then stop.
            await asyncio.shield(journal.record_finish(
                self.sf, call_id, status="unknown_outcome", duration_ms=int((time.monotonic() - start) * 1000),
                error=f"{tool.name} was cancelled while running; outcome unknown"))
            raise
        except ToolError as exc:
            # A write whose request may already have left (timeout, dropped connection) is not "failed": it may
            # have happened. unknown_outcome makes a repeat go through the replay-safe / human rules.
            status, error = ("unknown_outcome", f"outcome unknown: {exc.message}") if is_write and exc.transient \
                else ("failed", exc.message)
        except TimeoutError:
            status, error = (("unknown_outcome", f"outcome unknown: {tool.name} timed out after {tool.timeout_seconds:g}s")
                             if is_write else ("failed", f"{tool.name} timed out after {tool.timeout_seconds:g}s"))
        except (ValidationError, ValueError) as exc:
            status, error = "failed", f"{tool.name} failed: {str(exc)[:300]}"
        except Exception as exc:  # never crash the task on a tool bug; log it
            logger.exception("tool.unexpected_error tool=%s", tool.name)
            status, error = "failed", f"{tool.name} failed unexpectedly: {type(exc).__name__}"
        duration = int((time.monotonic() - start) * 1000)
        result = self._truncate(result, tool)
        warnings = await self._scan(tool, result, call_id) if status == "succeeded" else []
        await journal.record_finish(self.sf, call_id, status=status, result=result, error=error, duration_ms=duration,
                                    secrets=self.credentials.secret_values(),
                                    **({"injection_warnings": warnings} if warnings else {}))
        logger.info("tool.%s tool=%s task=%s step=%d ms=%d", status, tool.name, self.rt.task_key, step_index, duration)
        if status == "succeeded" and self.rt.settings.app_env != "production":
            from app.engine import chaos

            await chaos.after_tool(self.sf, self.rt.workflow_id, self.rt.task_key, tool.name, self.rt.settings)
        return StepView(index=step_index, tool=tool.name, arguments=args.model_dump(mode="json"), status=status,
                        result=result, error=error, untrusted=tool.untrusted,
                        source=_source_label(tool.name, args.model_dump(mode="json")), call_id=call_id)

    async def _scan(self, tool: Tool, result: dict[str, Any] | None, call_id: uuid.UUID) -> list[str]:
        """Flag (never block) instruction-like text in untrusted results: journal row, event, approval payload."""
        if not tool.untrusted or not result:
            return []
        from app.core.events import EventType, record_event
        from app.tools.injection import scan

        found = scan(result)
        if not found:
            return []
        source = str(result.get("final_url") or result.get("url") or tool.name)
        for name in found:
            note = f"{name} ({source})"
            if note not in self.state.injection_warnings:
                self.state.injection_warnings.append(note)
        async with self.sf() as s:
            record_event(s, workflow_id=self.rt.workflow_id, task_id=self.rt.task_id,
                         event_type=EventType.TOOL_INJECTION_SUSPECTED,
                         payload={"task_key": self.rt.task_key, "tool": tool.name, "source": source,
                                  "rules": found, "tool_call_id": str(call_id)})
            await s.commit()
        logger.warning("tool.injection_suspected task=%s tool=%s source=%s rules=%s", self.rt.task_key, tool.name,
                       source, found)
        return found

    def _truncate(self, result: dict[str, Any] | None, tool: Tool) -> dict[str, Any] | None:
        if result is None:
            return None
        limit = min(tool.max_output_chars, self.rt.settings.max_tool_output_chars) * 2
        for k, v in list(result.items()):
            if isinstance(v, str) and len(v) > limit:
                result[k] = v[:limit] + f" ... [truncated {len(v) - limit} chars]"
        return result

    def _needs_approval(self, tool: Tool, side_effect: str) -> bool:
        mode = approval_mode(tool, self.rt.config)
        if self.state.tainted and side_effect == "external_write":
            return True  # code-enforced, not configurable: untrusted content was read in this task
        return mode == "always" or (mode == "external_write_only" and side_effect == "external_write")

    async def _suspend(self, tool: Tool, args: BaseModel, reason: str | None, step_index: int, key: str,
                       warning: str | None = None) -> None:
        args_dict = args.model_dump(mode="json")
        side = tool.side_effect_for(args)
        call_id = await journal.record_step(
            self.sf, secrets=self.credentials.secret_values(),
            **self._base_row(step_index, tool.name, side, status="awaiting_approval", arguments=args_dict,
                             args_hash=journal.args_hash(args_dict), idempotency_key=key, reason=reason,
                             injection_warnings=self.state.injection_warnings or None))
        self.state.pending_call_id = call_id
        self.state.step_index = step_index + 1
        payload = {"kind": "tool_call", "tool": tool.name, "arguments": args_dict, "reason": reason,
                   "side_effect": side, "tainted": self.state.tainted,
                   "untrusted_sources": self.state.untrusted_sources[:20],
                   "injection_warnings": self.state.injection_warnings, "warning": warning,
                   "input_schema": tool.input_model.model_json_schema()}
        raise ToolApprovalNeeded(call_id, payload, self.state)

    def _exfil_warning(self, tool: Tool, args: BaseModel, side: str) -> str | None:
        """Reads in a tainted or private-data attempt may only go to trusted URLs (see app/tools/exfil.py)."""
        if tool.name not in READ_URL_TOOLS or side != "external_read" or not (self.state.tainted or self.private):
            return None
        url = str(getattr(args, "url", ""))
        query = getattr(args, "query", None)
        if query:
            url += ("&" if "?" in url else "?") + urlencode(query)
        options = self._options(tool)
        return check_read_url(url, ExfilContext(
            provenance=self.provenance, private_tokens=self.private_tokens,
            read_allowlist=list(options.get("read_allowlist", [])),
            max_url_length=self.rt.settings.exfil_max_url_length,
            max_query_chars=self.rt.settings.exfil_max_query_chars))

    async def _budget_check(self) -> None:
        from app.services import budget_service

        async with self.sf() as s:
            from app.models.workflow import Workflow

            wf = await s.get(Workflow, self.rt.workflow_id)
            limit = (budget_service.effective_budget(wf.budget, self.rt.settings) if wf else {}).get("max_tool_calls")
            if limit is None:
                return
            used = await journal.executed_tool_calls(s, self.rt.workflow_id)
        if used >= limit:
            await budget_service.pause_for_budget(self.sf, self.rt.workflow_id, "max_tool_calls", self.rt.settings,
                                                  estimated=True)
            raise BudgetPause("max_tool_calls")

    async def _tool_step(self, step: BaseModel, step_index: int) -> None:
        name, raw_args, reason = step.tool, step.arguments or {}, step.reason  # type: ignore[attr-defined]
        tool = self.tools.get(name or "")
        if tool is None:
            allowed = ", ".join(self.tools) or "none"
            view_id = await journal.record_step(self.sf, **self._base_row(
                step_index, (name or "?")[:64], "none", status="rejected_invalid", arguments=raw_args, reason=reason,
                error=f"tool {name!r} is not available to this agent (allowed: {allowed})"))
            self._absorb(StepView(index=step_index, tool=(name or "?")[:64], arguments=raw_args,
                                  status="rejected_invalid", call_id=view_id,
                                  error=f"tool {name!r} is not available to this agent (allowed: {allowed})"))
            return
        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            msg = "invalid arguments: " + "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:6])
            view_id = await journal.record_step(self.sf, **self._base_row(
                step_index, tool.name, tool.side_effect, status="rejected_invalid", arguments=raw_args, reason=reason,
                error=msg))
            self._absorb(StepView(index=step_index, tool=tool.name, arguments=raw_args, status="rejected_invalid",
                                  error=msg, call_id=view_id))
            return
        args_dict = args.model_dump(mode="json")
        side = tool.side_effect_for(args)
        h = journal.args_hash(args_dict)
        key = journal.idempotency_key(self.rt.workflow_id, self.rt.task_key, tool.name, h)
        async with self.sf() as s:
            prev = await journal.find_previous(s, key)
        decision = journal.reuse_decision(prev, side, tool.replay_safe(self._options(tool)), utcnow(),
                                          self.rt.settings.tool_result_reuse_seconds)
        if decision == "reuse" and prev is not None:
            row_id = await journal.record_step(self.sf, **self._base_row(
                step_index, tool.name, side, status="succeeded", arguments=args_dict, args_hash=h, idempotency_key=key,
                reason=reason, result=prev.result, reused_from_id=prev.id, duration_ms=0))
            view = StepView(index=step_index, tool=tool.name, arguments=args_dict, status="succeeded",
                            result=prev.result, untrusted=tool.untrusted, source=_source_label(tool.name, args_dict),
                            reused=True, call_id=row_id)
            self._absorb(view)
            return
        warning = None
        exfil = self._exfil_warning(tool, args, side)
        if exfil:
            await self._suspend(tool, args, reason, step_index, key, warning=exfil)
        if decision in ("unknown_reexecute", "unknown_needs_approval") and prev is not None:
            await journal.set_status(self.sf, prev.id, "superseded", finished_at=utcnow(),
                                     error="outcome unknown; replaced by a new call")
            warning = "A previous attempt may have sent this (its outcome is unknown)."
        if decision == "unknown_needs_approval" or self._needs_approval(tool, side):
            await self._suspend(tool, args, reason, step_index, key, warning=warning)
        await self._budget_check()
        call_id = await journal.record_start(self.sf, secrets=self.credentials.secret_values(), **self._base_row(
            step_index, tool.name, side, arguments=args_dict, args_hash=h, idempotency_key=key, reason=reason))
        self._absorb(await self._run(tool, args, call_id, key, step_index))

    # ------------------------------------------------------------------ main loop
    def _account(self, resp: Any) -> None:
        r = self.report
        r.steps += 1
        r.model, r.provider = resp.model, getattr(resp, "provider", "") or r.provider
        r.input_tokens += resp.input_tokens
        r.output_tokens += resp.output_tokens
        r.latency_ms += resp.latency_ms
        cost = getattr(resp, "cost_usd", None)
        r.cost_usd = None if (r.cost_usd is None or cost is None) else r.cost_usd + cost
        r.fallback_from = getattr(resp, "fallback_from", None) or r.fallback_from
        r.route_position = getattr(resp, "route_position", 0)

    async def run(self) -> tuple[BaseModel, LoopReport]:
        await self._restore()
        tools = list(self.tools.values())
        step_model = build_step_model(self.agent.output_model, list(self.tools))
        system = "\n\n".join([self.agent.build_system_prompt(), PROTOCOL, tools_section(tools), UNTRUSTED_RULE])
        base_prompt = self.agent.build_user_prompt(self.input)
        step_index = self.state.step_index
        max_steps = self.rt.settings.max_tool_steps
        forced = False
        while True:
            forced = step_index >= max_steps
            prompt = base_prompt + "\n\n" + steps_section(self.history, self.rt.settings.max_tool_output_chars)
            if forced:
                prompt += "\n\n" + FINAL_NOW
            resp = await self.agent.llm.generate_structured(
                system_prompt=system, user_prompt=prompt, output_model=step_model, use_search=False,
                thinking_level=self.agent.resolve_thinking_level(), model=self.agent.resolve_model(),
                role=self.agent.agent_type)
            self._account(resp)
            step = resp.data
            if step.action == "final" and step.output is not None:  # type: ignore[attr-defined]
                self.report.tainted = self.state.tainted
                self.report.untrusted_sources = list(self.state.untrusted_sources)
                self.report.injection_warnings = list(self.state.injection_warnings)
                self.report.calls = [{"tool": v.tool, "status": v.status, "arguments": v.arguments, "result": v.result,
                                      "error": v.error, "call_id": v.call_id, "reused": v.reused} for v in self.history]
                return step.output, self.report  # type: ignore[attr-defined]
            if forced:
                raise LLMOutputError(f"{self.agent.agent_type} did not return a final answer after "
                                     f"{max_steps} tool steps")
            if step.action == "final":  # final without output: tell the model
                view_id = await journal.record_step(self.sf, **self._base_row(
                    step_index, "final", "none", status="rejected_invalid",
                    error="action 'final' needs a complete 'output' object"))
                self._absorb(StepView(index=step_index, tool="final", arguments=None, status="rejected_invalid",
                                      error="action 'final' needs a complete 'output' object", call_id=view_id))
            else:
                await self._tool_step(step, step_index)
            step_index += 1


async def run_tool_loop(agent: BaseAgent, agent_input: AgentInput, rt: ToolRuntime) -> tuple[BaseModel, LoopReport]:
    return await ToolLoop(agent, agent_input, rt).run()


async def resume_info(session: AsyncSession, task_id: uuid.UUID, current_attempt: int) -> ResumeInfo | None:
    """Earlier consecutive attempts that ended without failing by themselves form the transcript."""
    from app.models.attempt import TaskAttempt

    rows = list(await session.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task_id,
                                                                TaskAttempt.attempt_number < current_attempt)
                                      .order_by(TaskAttempt.attempt_number.desc())))
    chain: list[int] = []
    state: LoopState | None = None
    for att in rows:
        if att.outcome not in RESUMABLE_OUTCOMES:
            break
        chain.append(att.attempt_number)
        if state is None and att.loop_state:
            state = LoopState.model_validate(att.loop_state)
    if not chain:
        return None
    return ResumeInfo(attempt_numbers=sorted(chain), state=state)


RESUMABLE_OUTCOMES = frozenset({"worker_lost", "interrupted", "budget_paused", "suspended", "running"})
