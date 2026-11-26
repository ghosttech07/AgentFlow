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
            if self.session_factory is not None and not await quota.try_reserve(
                    self.session_factory, entry, self.routes.quotas.get(entry), self.settings):
                skipped_quota = True  # used up / window exhausted (logged once per window by quota)
                continue
            if not self.breaker.allow(entry):  # half-open and its single trial is already in flight
                continue
            fallback_from = chain[0] if position > 0 else None
            started = time.perf_counter()
            try:
                response = await adapter.generate(model_name, system_prompt, user_prompt, output_model,
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
            await self._record(ctx, role, provider_name, model_name, "succeeded", None, response.input_tokens,
                               response.output_tokens, response.cost_usd, response.latency_ms, position,
                               fallback_from)
            await self._budget_after(ctx)
            return response

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
                      latency_ms: int | None, position: int, fallback_from: str | None) -> None:
        if self.session_factory is None:
            return
        async with self.session_factory() as session:
            session.add(LLMCall(workflow_id=ctx.workflow_id if ctx else None, task_id=ctx.task_id if ctx else None,
                                role=role, provider=provider, model=model, outcome=outcome, error_type=error_type,
                                input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
                                latency_ms=latency_ms, route_position=position, fallback_from=fallback_from))
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
