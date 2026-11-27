"""ModelRouter (Part 8): implements LLMClient, so agents are unchanged. Code decides everything:

for each call, walk the role's route ("provider:model" entries, config/models.yaml) in order:
  - skip an entry whose circuit is open (per provider:model) or whose quota window is used up
    (persisted, app.llm.quota; reserved atomically before the call, failed calls count too);
  - fall back to the next entry on overloaded, quota_exhausted (window marked exhausted),
    model-not-found and provider timeouts;
  - stop at once on output errors (after the adapter's single repair) and auth errors (ERROR log:
    misconfiguration); other transient errors are left to the task retry policy;
  - every real call writes one llm_calls row (provider, model, tokens, cost, latency, outcome,
    route position, fallback_from): the source of totals, budgets and /metrics/summary;
  - workflow budgets (app.services.budget_service): before a task's call, an already-reached limit or
    a pre-call cost estimate over the remaining budget pauses the workflow and raises BudgetPause;
    after any call (planner included), a reached limit pauses the workflow (graceful).
All entries fail -> LLMOverloadedError if any was overloaded, else the last error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.llm import quota
from app.llm.base import LLMClient, LLMResponse
from app.llm.circuit import CircuitBreaker
from app.llm.context import CallContext, call_context
from app.llm.errors import (
    BudgetPause,
    LLMAuthError,
    LLMError,
    LLMModelNotFoundError,
    LLMOutputError,
    LLMOverloadedError,
    LLMQuotaExhaustedError,
    LLMTimeoutError,
)
from app.llm.pricing import Pricing
from app.llm.routes import RouteConfig, load_routes, split_entry
from app.models.usage import LLMCall
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)

FALLBACK_ERRORS = (LLMOverloadedError, LLMQuotaExhaustedError, LLMModelNotFoundError, LLMTimeoutError)
LEGACY_FALLBACK_ERRORS = (LLMOverloadedError, LLMModelNotFoundError)  # the v1 GeminiClient rule


def entry_timeout(entry: str, settings: Settings) -> float:
    return settings.gemini_timeout_seconds if entry.startswith("gemini:") else settings.llm_timeout_seconds


def check_timeouts(settings: Settings, routes: RouteConfig) -> None:
    """Warn when a slow first entry would leave no task time for its fallback (TASK_TIMEOUT_SECONDS)."""
    for role, chain in routes.routes.items():
        if len(chain) < 2:
            continue
        needed = entry_timeout(chain[0], settings) + entry_timeout(chain[1], settings)
        if settings.task_timeout_seconds <= needed:
            logger.warning("router.timeout_budget role=%s: TASK_TIMEOUT_SECONDS=%s leaves no room for a fallback "
                           "after %s times out (%ss + %ss for the next entry); raise the task timeout or lower "
                           "LLM_TIMEOUT_SECONDS / GEMINI_TIMEOUT_SECONDS", role, settings.task_timeout_seconds,
                           chain[0], entry_timeout(chain[0], settings), entry_timeout(chain[1], settings))


class ModelRouter:
    def __init__(self, *, providers: dict[str, Any] | None, routes: RouteConfig | None, pricing: Pricing | None,
                 settings: Settings, session_factory: async_sessionmaker[AsyncSession] | None = None,
                 breaker: CircuitBreaker | None = None, fake: LLMClient | None = None) -> None:
        self.providers = providers
        self.routes = routes
        self.pricing = pricing
        self.settings = settings
        self.session_factory = session_factory
        self.breaker = breaker or CircuitBreaker(settings.model_circuit_failure_threshold,
                                                 settings.model_circuit_cooldown_seconds)
        self._fake = fake

    async def ensure_started(self) -> None:
        """Build adapters, validate routes (RouteConfigError aborts startup) and load prices once."""
        if self.providers is not None and self.routes is not None and self.pricing is not None:
            return
        from app.config import PROJECT_ROOT
        from app.llm.providers.registry import build_providers

        self.providers = await build_providers(self.settings, fake=self._fake)
        self.routes = load_routes(self.settings, set(self.providers))
        path = self.settings.pricing_config_path
        self.pricing = Pricing.load(path if path.is_absolute() else PROJECT_ROOT / path)
        check_timeouts(self.settings, self.routes)
        logger.info("router.ready providers=%s routes=%s", sorted(self.providers),
                    {role: entries for role, entries in self.routes.routes.items()})

    # ------------------------------------------------------------------ LLMClient
    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        use_search: bool = False,
        thinking_level: str | None = None,
        model: str | None = None,  # legacy per-call model hint: routes decide now
        role: str | None = None,
    ) -> LLMResponse:
        await self.ensure_started()
        assert self.routes is not None and self.providers is not None
        ctx = call_context.get()
        chain = self.routes.route_for(role)
        prompt_chars = len(system_prompt) + len(user_prompt)
        await self._budget_before(ctx, role, chain[0], prompt_chars, estimate=False)

        errors: list[LLMError] = []
        skipped_quota = False
        privacy_skips: list[str] = []  # Part 12: entries skipped because the prompt's PII may not go there
        team = await self._team(ctx)
        # Legacy (no models.yaml) keeps the v1 rule: fall back only on overload and model-not-found.
        fallback_on = LEGACY_FALLBACK_ERRORS if self.routes.legacy else FALLBACK_ERRORS
        for position, entry in enumerate(chain):
            if self.breaker.state(entry) == "open":
                logger.warning("llm.entry_skipped entry=%s reason=circuit_open", entry)
                continue
            # Budget and quota first, so a pause or a full window never consumes a half-open trial.
            await self._budget_before(ctx, role, entry, prompt_chars, estimate=True)
            provider_name, model_name = split_entry(entry)
            adapter = self.providers.get(provider_name)
            if adapter is None:  # only on the unfiltered legacy route (no key): fail exactly as before
                logger.error("llm.auth_failed entry=%s: provider not configured (missing API key)", entry)
                raise LLMAuthError(f"Provider '{provider_name}' is not configured (missing API key)")
            from app.privacy import guard

            prot = await guard.protect(self.session_factory, workflow_id=ctx.workflow_id if ctx else None, team=team,
                                       agent_type=role, provider=provider_name, model=model_name,
                                       system_prompt=system_prompt, user_prompt=user_prompt, settings=self.settings)
            if prot.skip_reason:
                privacy_skips.append(f"{entry}: {prot.skip_reason}")
                logger.warning("llm.entry_skipped entry=%s reason=privacy (%s)", entry, prot.skip_reason)
                await self._privacy_event(ctx, "privacy.secret_blocked" if "SECRET" in prot.skip_reason
                                          else "privacy.routed_away", entry, prot)
                continue
            if self.session_factory is not None and not await quota.try_reserve(
                    self.session_factory, entry, self.routes.quotas.get(entry), self.settings):
                skipped_quota = True  # used up / window exhausted (logged once per window by quota)
                continue
            if not self.breaker.allow(entry):  # half-open and its single trial is already in flight
                continue
            fallback_from = chain[0] if position > 0 else None
            started = time.perf_counter()
            try:
                response = await adapter.generate(model_name, prot.system_prompt, prot.user_prompt, output_model,
                                                  use_search=use_search, thinking_level=thinking_level)
            except asyncio.CancelledError:  # task timeout / lease lost / shutdown: release, record, re-raise
                self.breaker.release(entry)
                await asyncio.shield(self._record(ctx, role, provider_name, model_name, "cancelled", None, 0, 0,
                                                  None, int((time.perf_counter() - started) * 1000), position,
                                                  fallback_from))
                raise
            except LLMError as exc:
                latency = int((time.perf_counter() - started) * 1000)
                await self._record(ctx, role, provider_name, model_name, "failed", exc.error_type, 0, 0, None,
                                   latency, position, fallback_from)
                if isinstance(exc, (LLMOverloadedError, LLMTimeoutError)):
                    self.breaker.record_failure(entry)  # unavailable
                else:
                    self.breaker.record_success(entry)  # it answered (quota, 404, auth, bad output, ...)
                if isinstance(exc, LLMQuotaExhaustedError) and self.session_factory is not None:
                    await quota.mark_exhausted(self.session_factory, entry, self.routes.quotas.get(entry),
                                               self.settings)
                if isinstance(exc, LLMAuthError):
                    logger.error("llm.auth_failed entry=%s: misconfiguration (check the API key); not falling "
                                 "back", entry)
                    raise
                if isinstance(exc, LLMOutputError) or not isinstance(exc, fallback_on):
                    raise
                errors.append(exc)
                logger.warning("llm.fallback entry=%s reason=%s next=%s", entry, exc.error_type,
                               chain[position + 1] if position + 1 < len(chain) else "none")
                continue

            self.breaker.record_success(entry)
            assert self.pricing is not None
            response.provider, response.model = provider_name, model_name
            response.cost_usd = self.pricing.cost(entry, response.input_tokens, response.output_tokens)
            response.route_position, response.fallback_from = position, fallback_from
            if fallback_from:
                logger.warning("llm.used_fallback entry=%s instead_of=%s role=%s", entry, fallback_from, role)
            manifest = self._manifest(role, provider_name, model_name, prot, response, system_prompt,
                                      thinking_level, use_search, position, fallback_from)
            call_id = await self._record(ctx, role, provider_name, model_name, "succeeded", None,
                                         response.input_tokens, response.output_tokens, response.cost_usd,
                                         response.latency_ms, position, fallback_from, manifest=manifest)
            if ctx is not None and ctx.workflow_id is not None and self.session_factory is not None:
                from app.audit import manifest as mf

                mode = self.settings.audit_store_content
                await mf.store(self.session_factory, ctx.workflow_id, call_id, "prompt",
                               prot.system_prompt + "\n\n" + prot.user_prompt, mode)
                await mf.store(self.session_factory, ctx.workflow_id, call_id, "response", response.raw_text or "", mode)
            if prot.types or privacy_skips:
                await self._privacy_event(ctx, "privacy.protected", entry, prot,
                                          skipped=[x.split(": ", 1)[0] for x in privacy_skips])
            await self._budget_after(ctx)
            return response

        if privacy_skips and not errors and not skipped_quota:
            from app.llm.errors import LLMPrivacyBlockedError

            await self._privacy_event(ctx, "privacy.blocked", chain[0], None, reasons=privacy_skips)
            raise LLMPrivacyBlockedError("No allowed provider for this prompt: " + "; ".join(privacy_skips)
                                         + ". Add a local model (Ollama) to this role's route, or change the policy.")
        overloaded = [e for e in errors if isinstance(e, LLMOverloadedError)]
        if overloaded:
            last = overloaded[-1]
            raise LLMOverloadedError(
                f"All models unavailable for role '{role or 'default'}' ({', '.join(chain)}): {last.message}",
                retry_after_seconds=last.retry_after_seconds or self.breaker.seconds_until_available(chain),
            )
        if errors:
            raise errors[-1]
        if skipped_quota:  # nothing was tried because every usable entry is out of quota: not retryable
            raise LLMQuotaExhaustedError(
                f"Every model for role '{role or 'default'}' is out of quota until its window resets "
                f"({', '.join(chain)})")
        raise LLMOverloadedError(
            f"All models for role '{role or 'default'}' have an open circuit ({', '.join(chain)})",
            retry_after_seconds=self.breaker.seconds_until_available(chain))

    # ------------------------------------------------------------------ usage + budgets
    async def _record(self, ctx: CallContext | None, role: str | None, provider: str, model: str, outcome: str,
                      error_type: str | None, input_tokens: int, output_tokens: int, cost: float | None,
                      latency_ms: int | None, position: int, fallback_from: str | None,
                      manifest: dict[str, Any] | None = None) -> int | None:
        if self.session_factory is None:
            return None
        async with self.session_factory() as session:
            row = LLMCall(workflow_id=ctx.workflow_id if ctx else None, task_id=ctx.task_id if ctx else None,
                          role=role, provider=provider, model=model, outcome=outcome, error_type=error_type,
                          input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
                          latency_ms=latency_ms, route_position=position, fallback_from=fallback_from,
                          manifest=manifest)
            session.add(row)
            await session.commit()
            return row.id

    async def _team(self, ctx: CallContext | None) -> str | None:
        if self.session_factory is None or ctx is None or ctx.workflow_id is None:
            return None
        from app.models.governance import Team

        async with self.session_factory() as session:
            return await session.scalar(select(Team.name).join(Workflow, Workflow.team_id == Team.id)
                                        .where(Workflow.id == ctx.workflow_id))

    def _manifest(self, role: str | None, provider: str, model: str, prot: Any, response: LLMResponse,
                  system_prompt: str, thinking_level: str | None, use_search: bool, position: int,
                  fallback_from: str | None) -> dict[str, Any]:
        from app.audit import manifest as mf
        from app.audit.canonical import sha256_hex

        template = mf.template_of(role)
        return {"provider": provider, "model": model, "model_version": getattr(response, "model", None),
                "parameters": {"thinking_level": thinking_level, "use_search": use_search, "temperature": None,
                               "max_tokens": None},
                "prompt_template_hash": sha256_hex(template) if template else sha256_hex(system_prompt),
                "rendered_prompt_hash": sha256_hex(prot.system_prompt + "\n\n" + prot.user_prompt),
                "response_hash": sha256_hex(response.raw_text or ""),
                "usage": {"input_tokens": response.input_tokens, "output_tokens": response.output_tokens},
                "latency_ms": response.latency_ms, "route_position": position, "fallback_from": fallback_from,
                "pii": {"types": prot.types, "actions": prot.actions, "counts": prot.counts, "rules": prot.rules}}

    async def _privacy_event(self, ctx: CallContext | None, event_type: str, entry: str, prot: Any,
                             skipped: list[str] | None = None, reasons: list[str] | None = None) -> None:
        if self.session_factory is None or ctx is None or ctx.workflow_id is None:
            return
        from app.core.events import record_event
        from app.privacy.guard import summary

        if prot is None:
            payload = {"entry": entry, "reasons": reasons or [], "decision": {"effect": "block"}}
        else:
            local = sorted(t for t, m in prot.actions.items() if m == "local_only") if skipped else []
            payload = {"entry": entry, "types": prot.types, "actions": prot.actions, "counts": prot.counts,
                       "rules": prot.rules, "skipped_entries": skipped or [],
                       "summary": summary(prot.counts, local) or ", ".join(prot.types),
                       "decision": {"effect": "protect" if event_type == "privacy.protected" else "block",
                                    "matched_rule_ids": prot.rules}}
        async with self.session_factory() as session:
            record_event(session, workflow_id=ctx.workflow_id, task_id=ctx.task_id, event_type=event_type,
                         payload=payload)
            await session.commit()

    async def _budget_before(self, ctx: CallContext | None, role: str | None, entry: str, prompt_chars: int,
                             *, estimate: bool) -> None:
        """Task calls only: a reached limit, or (estimate=True) a priced call that would cross max_cost_usd,
        pauses the workflow BEFORE calling. Planner calls are never blocked, only counted."""
        if self.session_factory is None or ctx is None or ctx.workflow_id is None or ctx.task_id is None:
            return
        from app.services import budget_service as bs

        async with self.session_factory() as session:
            wf = await session.get(Workflow, ctx.workflow_id)
            if wf is None:
                return
            eff = bs.effective_budget(wf.budget, self.settings)
            if not estimate:
                which = bs.exceeded(eff, await bs.get_usage(session, wf))
                if which is None:
                    return
                estimated = False
            else:
                if eff.get("max_cost_usd") is None or self.pricing is None:
                    return
                output = await bs.typical_output_tokens(session, role) or self.settings.default_estimated_output_tokens
                cost = self.pricing.cost(entry, prompt_chars // 4, output)
                usage = await bs.get_usage(session, wf)
                if cost is None:  # a cost limit cannot be enforced on an unpriced paid model: stop, don't guess
                    logger.warning("budget.unpriced_entry workflow=%s entry=%s max_cost_usd=%s: add its price to "
                                   "pricing.yaml or remove the cost limit", wf.id, entry, eff["max_cost_usd"])
                elif usage.cost_usd + cost <= eff["max_cost_usd"]:
                    return
                which, estimated = "max_cost_usd", True
                logger.warning("budget.estimate_guard workflow=%s entry=%s spent=%.6f estimate=%s max=%s",
                               wf.id, entry, usage.cost_usd, "unpriced" if cost is None else f"{cost:.6f}",
                               eff["max_cost_usd"])
        await bs.pause_for_budget(self.session_factory, ctx.workflow_id, which, self.settings, estimated=estimated)
        raise BudgetPause(which)

    async def _budget_after(self, ctx: CallContext | None) -> None:
        if self.session_factory is None or ctx is None or ctx.workflow_id is None:
            return
        from app.services import budget_service as bs

        async with self.session_factory() as session:
            wf = await session.get(Workflow, ctx.workflow_id)
            if wf is None:
                return
            which = bs.exceeded(bs.effective_budget(wf.budget, self.settings), await bs.get_usage(session, wf))
        if which:
            await bs.pause_for_budget(self.session_factory, ctx.workflow_id, which, self.settings,
                                      current_task_id=ctx.task_id)
