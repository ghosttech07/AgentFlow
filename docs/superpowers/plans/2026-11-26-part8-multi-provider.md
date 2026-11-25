# Part 8: Multi-provider AI + Cost Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every agent role over an ordered `provider:model` chain (Gemini, Anthropic, OpenAI, Ollama, fake) with cross-provider fallback, per-call cost, per-workflow budgets that pause, and persisted per-provider quotas.

**Architecture:** One `ModelRouter` implements the existing `LLMClient` protocol and owns fallback, breaker, quota and budget decisions; provider adapters only translate one call. Every real call is one `llm_calls` row (the single source for totals, budgets, metrics). Fake mode is a provider behind the router, so budgets work in demos.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async/asyncpg, Alembic, Pydantic v2, google-genai, anthropic 2.14, openai 3.59 (also for Ollama's OpenAI-compatible endpoint), PyYAML, zoneinfo/tzdata.

**Spec:** `docs/superpowers/specs/2026-11-26-part8-multi-provider-design.md` + the Part 8 prompt (CLAUDE.md, AgentFlow 2.0).

## Global Constraints
- Only GEMINI_API_KEY + no models.yaml => identical behaviour; all existing tests pass unchanged (except where they assert lists that legitimately grow).
- LLM_PROVIDER=fake overrides every route (all roles -> `fake:fake-model`).
- Routing, fallback, budgets, quotas decided by code only.
- No real network in the default suite; live tests behind markers `live_gemini|live_anthropic|live_openai|live_ollama`.
- Never log or return API keys; `/providers` shows only `key_present`.
- Unknown price -> cost `null` + one WARNING per provider:model; never guess.
- DEFAULT_MAX_LLM_CALLS=50; other default limits unlimited.
- Ollama base URL default `http://localhost:11434/v1`; QUOTA_TIMEZONE default `UTC` (per-provider override).

## Review Focus
1. A route entry for a provider that is not configured (no key / Ollama down) -> skipped with one WARNING, never a crash; no usable entry for a role -> startup error naming the role.
2. Budget reached mid-workflow while another task is in flight -> workflow PAUSED (graceful), in-flight result still saved, nothing new claimed, and the paused workflow stays paused after restart.
3. Pre-call estimate exceeds remaining budget -> pause BEFORE the call; the task returns to READY without using a retry (not FAILED).
4. Two engines incrementing the same quota window concurrently -> exact count (atomic upsert), never over the limit by more than in-flight calls.
5. Provider returns quota_exhausted while the local counter says there is room -> window marked exhausted, router stops trying it until the window rolls over.

---

### Task 1: Error taxonomy, settings, LLMResponse fields
**Files:** Modify `app/llm/errors.py`, `app/llm/base.py`, `app/config.py`, `.env.example`. Test: `tests/test_llm_errors.py` (extend).
**Produces:**
- `LLMAuthError(LLMPermanentError)` (error_type `permanent`, code `llm_auth`), `LLMTimeoutError(LLMTransientError)` (error_type `timeout`), `BudgetPause(Exception)` (not an LLMError; carries `reason: str`).
- `LLMResponse` gains `provider: str = "gemini"`, `cost_usd: float | None = None`, `fallback_from: str | None = None`, `route_position: int = 0`.
- Settings: `anthropic_api_key`, `openai_api_key` (SecretStr|None), `ollama_base_url="http://localhost:11434/v1"`, `llm_timeout_seconds=120`, `models_config_path=Path("config/models.yaml")`, `pricing_config_path=Path("config/pricing.yaml")`, `quota_timezone="UTC"`, `default_max_cost_usd|default_max_tokens|default_max_duration_seconds: None`, `default_max_llm_calls: int|None = 50`, `default_estimated_output_tokens=1500`.
- [ ] Tests: Gemini timeout classifies to LLMTimeoutError (error_type "timeout"); 401/403 -> LLMAuthError.
- [ ] Implement; run `pytest tests/test_llm_errors.py tests/test_errors_classification.py` -> PASS.

### Task 2: Provider adapters + registry
**Files:** Create `app/llm/providers/{__init__,base,gemini,openai_provider,anthropic_provider,ollama_provider,registry}.py`; `app/llm/gemini_client.py` becomes a thin legacy shim re-exporting the Gemini adapter (keeps old imports/tests). Test: `tests/test_providers.py`.
**Produces:**
- `class ProviderAdapter(Protocol)`: `name: str`; `async def generate(self, model: str, system_prompt: str, user_prompt: str, output_model: type[BaseModel], *, use_search: bool, thinking_level: str | None) -> LLMResponse` (one model, no fallback; raises taxonomy errors; one repair attempt via shared `parse_or_repair`).
- `base.py`: `parse_json(raw, output_model, provider) -> BaseModel` (fence-tolerant, raises LLMOutputError), `repair_prompt(...)`, `status_error(provider, status, message, retry_after) -> LLMError` mapping 400->Permanent, 401/403->Auth, 404->ModelNotFound, 408->Timeout, 429->Transient or QuotaExhausted (quota words: "quota", "billing", "spend limit", "insufficient_quota", "per day"), 500/502/504->Transient, 503/529->Overloaded.
- Gemini: existing `_generate_with_model` logic unchanged; `classify_exception` reused.
- OpenAI-compatible (`OpenAICompatProvider(name, client, priced=True)`): `client.chat.completions.parse(model, messages=[system,user], response_format=output_model, reasoning_effort=<mapped>)`; drop `reasoning_effort` for that model after a 400 mentioning it (remember per model). thinking map: minimal/low->"low", medium->"medium", high->"high". Map `openai.*Error` via status_error (+`insufficient_quota` code -> QuotaExhausted, APITimeoutError -> LLMTimeoutError, APIConnectionError -> Transient).
- Ollama: `OpenAICompatProvider("ollama", AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama"))`; registered only if `GET {base}/models` answers within 2 s at startup; connection refused at call time -> LLMTransientError.
- Anthropic: `client.messages.parse(model, max_tokens=16000, system, messages, output_format=output_model, output_config={"effort": <low|medium|high>})` -> `parsed_output`; errors: `OverloadedError`->Overloaded, `RateLimitError`->Transient(retry-after) unless spend/quota words or `EnforcedSpendLimitReachedError`/billing classes->QuotaExhausted, `AuthenticationError|PermissionDeniedError`->Auth, `NotFoundError`->ModelNotFound, `APITimeoutError`->Timeout; `stop_reason=="refusal"` -> LLMPermanentError.
- `registry.build_providers(settings, *, fake: LLMClient | None = None) -> dict[str, ProviderAdapter]`: gemini/openai/anthropic only when their key exists; ollama when reachable; INFO line per skipped provider; `fake` provider wraps a FakeLLMClient (provider "fake", cost 0).
- [ ] Tests (mocked SDK objects, realistic payloads): each adapter returns validated `LLMResponse` with provider/model/tokens; invalid-then-valid JSON uses one repair; each provider's 429-rate, 429-quota, 503/529, 401, 404, timeout map to the right class with `retry_after_seconds`; missing key -> not registered.
- [ ] Implement; `pytest tests/test_providers.py tests/test_model_fallback.py tests/test_llm_errors.py` -> PASS.

### Task 3: Pricing
**Files:** Create `config/pricing.yaml`, `app/llm/pricing.py`. Test: `tests/test_pricing.py`.
**Produces:** `load_pricing(path) -> dict[str, tuple[float, float]]`; `cost_usd(entry: str, input_tokens: int, output_tokens: int) -> float | None` (ollama:* and fake:* -> 0.0; unknown -> None + one WARNING per entry).
- [ ] Tests: known price math (per 1M tokens), ollama 0, unknown -> None and exactly one warning across two calls.

### Task 4: Routing config (models.yaml)
**Files:** Create `config/models.example.yaml`, `app/llm/routes.py`; script `scripts/list_provider_models.py`. Test: `tests/test_routes_config.py`.
**Produces:** `RouteConfig` (Pydantic): `routes: dict[str, list[str]]` (`default` required), `quotas: dict[str, QuotaSpec(requests_per_day: int|None, requests_per_minute: int|None, timezone: str|None)]`. `load_routes(settings, available: set[str]) -> RouteConfig` (missing file -> legacy: default = gemini:GEMINI_MODEL + fallbacks; worker roles researcher/analyst/writer/reviewer/ask = gemini:GEMINI_WORKER_MODEL first when set). Validation errors -> `RouteConfigError` (clear message). Entries of unavailable providers dropped with one WARNING; a role left empty -> `RouteConfigError("No usable model for role 'X' ...")`. `route_for(role) -> list[str]`.
- [ ] Tests: invalid YAML / unknown provider / bad "provider:model" / empty route -> RouteConfigError; missing file -> legacy route; unconfigured provider skipped with warning; no usable entry -> error; per-role route vs default.

### Task 5: Migration 0005 + usage models
**Files:** Create `alembic/versions/20261126_0005_cost_control.py`, `app/models/usage.py`; modify `app/models/workflow.py` (`budget: JSONB|None`), `app/models/attempt.py` (`cost_usd: Numeric(12,6)|None`, `provider: String(32)|None`). Tables: `llm_calls` (id bigint identity, workflow_id FK null cascade, task_id FK null cascade, role, provider, model, outcome succeeded|failed, error_type, input_tokens, output_tokens, cost_usd Numeric null, latency_ms, fallback_from, route_position, created_at; index (workflow_id)), `provider_usage` (provider, model, window_kind 'day'|'minute', window_start timestamptz, request_count int, exhausted bool; unique (provider, model, window_kind, window_start)).
- [ ] `alembic upgrade head` on main DB; `pytest tests/test_migrations.py` -> PASS (no drift).

### Task 6: Quotas
**Files:** Create `app/llm/quota.py`. Test: `tests/test_quotas.py`.
**Produces:** `window_start(kind, now, tz) -> datetime`; `async try_reserve(sf, entry, spec, now) -> bool` (atomic `INSERT ... ON CONFLICT DO UPDATE SET request_count = request_count + 1 RETURNING request_count, exhausted`; False when exhausted or count > limit, and logs once per window); `async mark_exhausted(sf, entry, spec, now)`; `async usage_today(sf, entry, spec) -> dict`.
- [ ] Tests: 20 concurrent reserves with limit 10 (two engines' session factories) -> exactly 10 True; day window rolls at midnight of the configured tz (America/Los_Angeles vs UTC); mark_exhausted blocks further reserves in that window only.

### Task 7: Router + call context + usage rows + budgets
**Files:** Create `app/llm/router.py`, `app/llm/context.py`, `app/services/budget_service.py`. Modify `app/llm/__init__.py` (`get_llm_client` returns the router; fake mode = router over the demo fake), `app/agents/base.py` (pass `role=self.agent_type`), `app/services/ask_service.py` (role "ask"), `app/engine/worker.py`, `app/engine/runner_jobs.py`, `app/services/planning_service.py` (set call context). Test: `tests/test_router.py`, `tests/test_budgets.py`.
**Produces:**
- `context.call_context: ContextVar[CallContext|None]`; `CallContext(workflow_id, task_id, session_factory)`; `with use_context(...)`.
- `ModelRouter(providers, routes, pricing, breaker, session_factory, settings)` with `generate_structured(system_prompt, user_prompt, output_model, use_search=False, thinking_level=None, model=None, role=None) -> LLMResponse`. Walk `route_for(role)`: skip open circuit / failed `try_reserve`; fallback on Overloaded, QuotaExhausted (also `mark_exhausted`), ModelNotFound, LLMTimeoutError; stop on LLMOutputError and LLMAuthError (log ERROR). Record one `llm_calls` row per real call (success or failure) when a context with a session_factory exists. Response gets provider, cost_usd, fallback_from (first entry tried), route_position. All failed -> Overloaded if any overloaded else last error.
- Budget: `budget_service.effective_budget(wf, settings) -> dict`; `async usage(session, workflow_id) -> Usage(cost_usd, input_tokens, output_tokens, llm_calls)` from `llm_calls`; `exceeded(budget, usage, started_at) -> str|None` (which limit); `async pause_for_budget(sf, workflow_id, which)` (reuses `control_service.pause(actor="budget", mode="graceful", reason=f"budget_exceeded: {which}")` + event `workflow.budget_exceeded`). Router: before a call with a workflow context: if already exceeded, or (max_cost set and spent + estimate > max) -> `pause_for_budget` then raise `BudgetPause`; after the call's row is written: if exceeded -> `pause_for_budget` (the current result is still returned and saved).
- Worker: `BudgetPause` -> close attempt outcome `budget_paused`, task RUNNING->READY with `max_attempts += 1` (same as an immediate-pause interruption), result discarded. Task metrics add provider, cost_usd, fallback_from, route_position; attempt row gets cost_usd + provider.
- Scheduler: `schedule_workflow` pauses a RUNNING/AA workflow whose budget is exceeded (incl. max_duration_seconds) before promoting/claiming.
- [ ] Router tests: per-role routes; overload on gemini -> answer from anthropic with fallback_from "gemini:..." and route_position 1; output error / auth error -> no fallback; open circuit skipped; exhausted quota skipped; all fail -> overloaded; llm_calls rows written (failed + succeeded).
- [ ] Budget tests (fake provider through router): pause on max_llm_calls / max_tokens / max_cost_usd / max_duration_seconds; pre-call estimate guard pauses before calling and the task is READY with attempt not consumed; planner calls counted; PATCH budget + resume -> COMPLETED; budget-paused workflow stays PAUSED after `recover()` + `run_until_idle()`.

### Task 8: API
**Files:** Modify `app/schemas/workflow.py` (`WorkflowCreate.budget: Budget|None`; detail: `budget`, `usage`, `remaining`), `app/api/routes_workflows.py` (PATCH `/workflows/{id}/budget` {actor, budget} with require_admin, event `workflow.budget_updated`), create `app/api/routes_providers.py` (`GET /providers`, `GET /metrics/summary`), `app/services/timeline_service.py` (summary `cost_by_provider`, `fallbacks`; span attempts carry provider/model/fallback_from). Test: `tests/test_cost_api.py`.
- [ ] Tests: POST with budget stored; detail shows usage/remaining; PATCH 401 without key when configured, event recorded; /providers never contains a key and shows circuit + today's usage vs quota; /metrics/summary totals per provider (calls, errors, avg latency, cost).

### Task 9: Dashboard
**Files:** `app/static/dashboard.html` (+ impeccable redesign round). Shows provider:model per attempt + "↪ fallback from X", budget bar + budget-paused banner with adjust form (PATCH), Providers panel, timeline cost by provider + fallback count.
- [ ] JS syntax check (`node --check`), `tests/test_timeline.py::test_dashboard_is_served` extended for `/providers` and `/budget`; headless screenshots desktop + 600px.

### Task 10: Live checks, demos, docs
- [ ] `scripts/list_provider_models.py` run; fill `config/models.example.yaml` (3 profiles) with models that exist; others marked placeholder.
- [ ] Live tests file `tests/live/test_providers_live.py` (markers per provider; Ollama also a 3-task workflow).
- [ ] `scripts/budget_demo.py` (fake): max_llm_calls=3 -> PAUSED -> PATCH -> resume -> COMPLETED, prints events.
- [ ] Full suite green; engine suites x3; crash/controls demos PASS; one live Gemini test; free-dev Ollama workflow.
- [ ] Update CLAUDE.md, README, docs/ARCHITECTURE.md; mark Part 8 done.
