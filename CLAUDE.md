# AgentFlow — project context for Claude

## Goal
AgentFlow is a backend multi-agent orchestration engine. A user submits a goal; a Planner agent
(Gemini) breaks it into a DAG of tasks; specialist agents (Researcher, Writer, Reviewer, ...) run
those tasks in dependency order (independent tasks in parallel). All workflow/task state is
persisted in PostgreSQL so nothing is lost on a crash. Failed tasks retry with backoff and
recover after a restart. Workflows support pause/resume/cancel and human approval checkpoints.

**Final demo:** run a workflow, deliberately crash an agent, restart the service, and show that
completed tasks are preserved and the failed task resumes.

## Roadmap (7 parts)
1. [x] **Foundation** — project setup, DB models, state machine, basic REST API, Docker (DONE)
2. [x] **Agents** — agent interface, Gemini client, specialist agents, agent registry
   (DONE)
3. [x] **Planning & delegation** — Planner turns a goal into a validated task DAG (DONE)
4. [x] **Execution engine** — dependency-aware scheduler, parallel worker pool (DONE)
5. [x] **Reliability** — retries, backoff, leases/heartbeats, crash recovery, idempotency (DONE)
6. [x] **Human controls** — pause/resume/cancel, approval checkpoints (DONE)
7. [x] **Observability & demo** — event timeline, dashboard/CLI, crash demo script, README (DONE)

Only build the part currently being worked on. Do not pull later-part features forward.

### AgentFlow 2.0 (Parts 8-15)
Positioning: a self-hosted engine for TRUSTWORTHY AI workflows. AI does the work; code guarantees
reliability, cost limits and safety; humans approve under policy; every step is provable.
8. [x] **Multi-provider AI + cost control**: Gemini, Claude, OpenAI, Ollama; cross-provider
   fallback; cost tracking; budgets; persisted quotas (DONE)
9. [x] **Tools + safe actions**: tool registry (web, HTTP APIs, DB, files, email), side-effect rules, sandboxing (DONE)
10. [x] **Adaptive workflows**: feedback loops, conditional branches, bounded re-planning, memory (DONE)
11. [x] **Policy engine + users/roles**: policy-as-code, RBAC, multi-approver (DONE)
12. [ ] **Trust layer**: tamper-evident hash-chained audit, reproducibility, PII redaction, compliance export
13. [ ] **Quality engine**: evals, quality gates, replay / what-if runs
14. [ ] **Real-time + scale**: SSE streaming, LISTEN/NOTIFY, multiple workers, shared breaker, load test
15. [ ] **Developer experience + launch**: YAML templates, Python SDK, plugins, final docs, v2 demo
Same rule: build one part, test it, then wait for the next part's prompt.

### Status
- Part 1 done (2026-11-21): 61 tests pass against Postgres 16 in Docker; `/health` and the
  workflow endpoints verified on a live uvicorn server.
- Part 2 done (2026-11-21): 113 default tests pass (FakeLLMClient, no network); 2 live tests
  pass against `gemini-3.5-flash`; real `POST /agents/researcher/run` verified.
- Model: `gemini-3.5-flash` (newest stable flash that answered; `gemini-2.5-flash` returns 404
  "no longer available to new users"; `gemini-3.8-flash` returned 503 overloaded). Calls take
  ~13-20s and ~1.2-1.5k output tokens (mostly thinking tokens).
- **Search grounding is not available on the current API key**: every grounded call returns 429
  RESOURCE_EXHAUSTED while plain calls work (plan/billing limit). Until billing is enabled, run
  with `ENABLE_SEARCH_GROUNDING=false`. The single-call grounded-JSON path (Gemini 3+) is
  therefore unit-tested only, not live-verified.
- Part 3 done (2026-11-24): default tests pass. Live planner test NOT yet run: Gemini
  `gemini-3.5-flash` returned 503 "high demand" on every call 2026-11-24 (provider outage).
- Part 4 done (2026-11-24): 189 default tests pass (engine driven with FakeLLMClient). Real-server
  smoke test verified engine startup, run=true background planning and clean FAILED on 503.
  Live engine test + demo run pending the same Gemini outage.
- Part 5 done (2026-11-25): 235 default tests pass (engine/reliability tests 3x green).
  `crash_demo.py --fake` PASS and `crash_demo.py` (live Gemini) PASS. Live tests 4/4 pass
  (2026-11-25, with ENABLE_SEARCH_GROUNDING=false), which closes the pending Part 3/4 live items.
  `run_workflow.py` not re-run separately (the live crash demo ran the same EV goal end to end).
- **Free-tier quota: `gemini-3.5-flash` allows 20 requests/day.** A hard per-day 429 is
  `quota_exhausted` (not retried, no fallback). Don't probe the primary in loops. Grounding is still
  unavailable, so `.env` should set `ENABLE_SEARCH_GROUNDING=false` (default is true).
- Part 6 done (2026-11-25): 300 default tests pass (engine/control suites 3x green, 82 each);
  `controls_demo.py` A/B/C PASS; `crash_demo.py --fake` PASS. Zero Gemini calls (fake only).
- Part 7 done (2026-11-25): 303 default tests pass; dashboard checked in headless Chrome (desktop +
  narrow); `crash_demo.py --fake` PASS with timeline. All 7 parts done.
- v1 extras (2026-11-26, user-requested): free live web research for the researcher, Quick Ask
  (`POST /ask`), today's date in every prompt, fast worker model (GEMINI_WORKER_MODEL), grounding
  default off. 311 tests pass; crash + controls demos PASS; live Quick Ask + a live 5-task workflow
  verified with real, cited Oct 2026 data. Part 7 confirmed.
- Free metasearch (ddgs) is flaky per engine ("No results found", throttling after bursts); the layer
  tries engines one by one (yahoo, bing, yandex, google). Tavily (free key) is the reliable upgrade.
- When a demo runs, stop any other server on the same DB first (two single-node engines conflict).
- Part 8 done (2026-11-26): 411 default tests pass (engine/control/cost suites 3x green, 122 each);
  live: Ollama structured call + full local workflow PASS, one Gemini call PASS, free-dev workflow
  COMPLETED at $0; budget/crash/controls demos PASS; whole-branch review fixes (7 Important) applied with
  regression tests. Next: Part 9 (tools + safe actions).
- Part 9 done (2026-11-26): 582 default tests (full run 581 passed + 1 timing-flaky retry test, fixed: tolerance
  1 s -> 5 s); engine/tool suites 3x (see logs/p9_final_verify.txt); tools_demo 12/12 PASS; crash/controls/budget
  demos PASS; live Gemini researcher tool loop PASS (3 calls, $0.004); final opus review: 1 Critical + 6 Important
  fixed with regression tests (tests/test_p9_review_fixes.py), 8 minors deferred (ledger). Also on this branch:
  dashboard redesign (answer engine), Quick Ask history, Part 7 audit (docs/superpowers/specs/part7-audit.md).
- Local model check (scripts/local_model_check.py): llama3.2:3b never calls tools (0/15); qwen3:4b uses tools but
  110-170 s/run + timeouts; qwen3.5:4b researcher + analyst used tools correctly, 0 invalid calls (1 run/role).
  Recommended routing in config/models.example.yaml: research/analysis on ollama:qwen3.5:4b, planner/writer/
  communicator on Gemini. Next: Part 10 (adaptive workflows). Open for the user: Docker "full" profile, CI + ruff.
- Part 10 done (2026-11-27): T1-T14 + final-review fix pass (I-1..I-7 + loop final_outputs, regression tests in
  tests/test_p10_review_fixes.py); adaptive_demo 12/12 PASS; see the Part 10 section. Next: Part 11 (governance).
- Part 11 done (2026-11-27): users/teams/roles, tenant isolation, policy-as-code + safety floor, multi-approver;
  tests/test_governance.py; governance_demo 12/12 PASS. Next: Part 12 (trust layer).
- Docker Desktop on this machine: if `docker` is not on PATH in a fresh shell, it lives at
  `C:\Users\DELL\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`.

## Tech stack
Python 3.12 (3.11+ supported) · FastAPI + Uvicorn · PostgreSQL 16 (Docker Compose) ·
SQLAlchemy 2.0 async + asyncpg · Alembic (async env) · Pydantic v2 + pydantic-settings ·
pytest + pytest-asyncio + httpx. Dependencies pinned to major versions in `requirements.txt`.

## Folder structure
```
app/
  main.py               FastAPI app factory, routers, error handlers, lifespan
  config.py             Settings (pydantic-settings, reads .env at project root)
  db/base.py            DeclarativeBase, naming convention, utcnow(), status_column_type()
  db/session.py         Async engine, SessionLocal, get_session() dependency
  models/               workflow.py, task.py (+ task_dependencies table), event.py, attempt.py,
                        approval.py (Approval, Publication)
  schemas/              Pydantic request/response models (workflow, task, event, common)
  core/enums.py         WorkflowStatus, TaskStatus (StrEnum; stored as VARCHAR + CHECK)
  core/state_machine.py Allowed transitions + transition_workflow()/transition_task()
  core/dag.py           Pure DAG rules: validate_plan, topological_order, execution_levels, mermaid
  core/events.py        EventType + record_event() helper
  core/errors.py        AppError, NotFoundError, ValidationFailedError, InvalidTransitionError
  core/logging.py       setup_logging(): "<ts> <LEVEL> <logger> <message>"
  api/                  routes_health.py, routes_workflows.py, routes_agents.py, routes_plans.py,
                        routes_approvals.py, routes_engine.py, routes_dev.py, routes_dashboard.py,
                        auth.py (X-API-Key)
  static/dashboard.html  Part 7 dashboard (served at /dashboard)
  services/             workflow_service.py (CRUD, graph), planning_service.py (plan + repair + persist),
                        control_service.py (pause/resume/cancel), approval_service.py, notify.py (webhook),
                        timeline_service.py (Part 7)
  llm/                  base.py (LLMClient protocol, LLMResponse, Source), router.py (ModelRouter),
                        routes.py (models.yaml), pricing.py, quota.py, context.py, circuit.py,
                        providers/ (gemini, anthropic, openai, ollama, fake, registry), gemini_client.py
                        (legacy), fake_client.py (tests), errors.py; get_llm_client() -> router
  agents/               base.py (BaseAgent, AgentInput, AgentResult), registry.py, prompts.py,
                        researcher.py, analyst.py, writer.py, reviewer.py, planner.py
  engine/               engine.py (Engine loop), scheduler.py (promote/gate/finish/claim/reap),
                        worker.py (run one task + heartbeat), context.py (AgentInput from DB),
                        runner_jobs.py (background planning), deps.py (get_engine),
                        retry.py (backoff, attempts), recovery.py (startup), chaos.py (dev faults)
scripts/list_models.py  Lists Gemini models usable with your key (generateContent)
scripts/run_workflow.py Demo: run a goal end to end against the running server, live table
scripts/crash_demo.py   Part 5 demo: crash the server mid-task, restart, verify recovery
scripts/controls_demo.py Part 6 demo: pause/resume, approve, reject, cancel (fake LLM)
scripts/budget_demo.py  Part 8 demo: budget pause -> PATCH -> resume -> COMPLETED (fake LLM)
scripts/tools_demo.py   Part 9 demo: tools, approval with edits, crash after email (exactly one), injection blocked
scripts/adaptive_demo.py Part 10 demo: loop 5 -> v2 -> 9, branches, re-plan, crash during v2, memory, exfil
scripts/demo.py         runs every demo and prints one PASS/FAIL table
scripts/setup_sample_data.py  read-only sample DB for sql_query (idempotent)
scripts/local_model_check.py  Ollama models vs the tool action protocol (success / tool use / valid steps)
scripts/list_provider_models.py  provider:model entries available to your keys / local Ollama
config/                 models.example.yaml (routes + quotas), pricing.yaml (USD per 1M tokens)
docs/ARCHITECTURE.md    architecture diagram incl. the router
alembic/                async env.py + versions/ (0001 initial schema, 0002 engine fields)
docker/postgres-init/   SQL run on first DB init (creates agentflow_test)
tests/                  conftest.py + API, state machine, health, migration-drift tests
```

## Data model (Part 1)
- `workflows`: UUID id, name, goal, status, context JSONB, result JSONB, error, timestamps.
- `tasks`: UUID id, workflow_id (cascade), key (unique per workflow), agent_type, status,
  input/output JSONB, requires_approval, attempt_count/max_attempts, idempotency_key,
  lease_owner, lease_expires_at, last_heartbeat_at, next_attempt_at (Part 5), timestamps.
  Index (workflow_id, status).
- `task_dependencies`: (task_id, depends_on_task_id) composite PK, CHECK no self-edge.
  ORM: `Task.dependencies` (upstream) / `Task.dependents` (downstream).
- `workflow_events`: bigint identity id, workflow_id, task_id?, event_type, payload JSONB,
  created_at. Append-only. Index (workflow_id, created_at).

## Agents & LLM (Part 2)
**Core rule: AI decides content, code decides control.** Agents only produce content; they never
touch the DB or change task/workflow status. The orchestrator (Part 4) will call `agent.run()`
and persist results through services + the state machine.

- `LLMClient.generate_structured(system_prompt, user_prompt, output_model, use_search)` returns an
  `LLMResponse` (validated `data`, raw_text, model, tokens, latency_ms, sources). `GeminiClient`
  uses `google-genai` (`client.aio.models.generate_content`) with `response_mime_type=json` +
  `response_json_schema=output_model.model_json_schema()`, then validates with Pydantic.
  It is created once (`app.llm.get_llm_client`, lru_cache); agents get it via their constructor.
- Timeout: `asyncio.wait_for(..., GEMINI_TIMEOUT_SECONDS)`. The SDK itself does no retries.
- Invalid output: exactly ONE in-client repair call (bad output + validation error sent back),
  then `LLMOutputError`. There are no other retry loops (retries are Part 5).
- Search grounding (researcher only, `ENABLE_SEARCH_GROUNDING`): `gemini-3*` models do
  grounding + JSON in one call; older models use two steps (grounded free text → JSON
  conversion). Sources come from `grounding_metadata.grounding_chunks[].web` and replace the
  researcher's self-reported `sources`.
- Code-enforced post-processing (`BaseAgent.postprocess`): writer `word_count` is recomputed;
  reviewer `approved` is forced false if any high-severity issue; researcher sources = grounding.
- Prompts (`agents/prompts.py`): sections OVERALL GOAL / YOUR TASK / TASK INPUT (JSON) /
  RESULTS FROM PREVIOUS TASKS (JSON per task key) / RULES. Dependency outputs are fairly
  truncated to `MAX_CONTEXT_CHARS` (short ones kept whole, long ones share the rest) with a note.
- Logging: model, latency, token usage at INFO; full prompts only at DEBUG. Never log the key.

### Error classes (`app/llm/errors.py`) — Part 5 retry policy depends on these
| Class | Cause | Retry? | Dev API status |
|---|---|---|---|
| Class (`error_type`) | Cause | Engine retries? | Dev API status |
|---|---|---|---|
| `LLMTransientError` (transient) | timeout, HTTP 408, per-minute 429, other 5xx, network | yes, backoff | 503 |
| `LLMOverloadedError` (overloaded, subclass of transient) | 503 / "high demand" | yes + model fallback | 503 |
| `LLMQuotaExhaustedError` (quota_exhausted) | 429 hard quota: per-day, limit 0, no RetryInfo | no | 429 |
| `LLMPermanentError` (permanent; `LLMModelNotFoundError` for 404) | 400/401/403/404, safety, missing key | no (404 → fallback) | 502 |
| `LLMOutputError` (output) | not JSON / schema mismatch after the one repair | yes | 502 |
`retry_after_seconds` is parsed from RetryInfo `retryDelay` or the Retry-After header. Engine-only
types: `timeout`, `unknown`, `worker_lost` (all retryable).
All are `AppError` subclasses, so the API renders them as `{"error": {...}}` automatically.

### Adding a new agent
1. Create `app/agents/<name>.py`: a Pydantic output model + a `BaseAgent` subclass with
   `agent_type`, `description`, `system_prompt`, `output_model` (and `use_search` if needed);
   decorate it with `@register_agent`. Override `postprocess` for code-enforced fields.
2. Import the module in `app/agents/__init__.py` (that import is what registers it).
3. Add it to the parametrized test in `tests/test_agents.py`; update the type lists asserted
   in `tests/test_registry.py` and `tests/test_agents_api.py`.
Workflow creation and the planner's catalog pick up the new `agent_type` automatically. Set
`sensitive = True` if its tasks must always require approval.

### API
- `GET /agents` — registered agents with description + JSON output schema.
- `POST /agents/{agent_type}/run` — dev-only standalone run (no tasks/DB); 404 when
  `APP_ENV=production`. Unknown agent → 404, transient → 503, permanent/output → 502.
- `POST /workflows` rejects tasks whose `agent_type` is not registered (422 listing valid types).

## Planning (Part 3)
**The AI proposes the plan; code validates it. An invalid plan never reaches the database.**

### DAG rules (`app/core/dag.py`, pure functions)
`validate_plan(tasks, valid_agent_types, max_tasks)` returns ALL errors (`PlanValidationError`
with `code`, `message`, `task_key`): 1..`MAX_PLAN_TASKS` tasks; keys lowercase snake_case
(`^[a-z][a-z0-9_]{0,49}$`) and unique; every `depends_on` key exists, no self-dependency, no
duplicate entries; no cycles (message shows the path, e.g. `a -> b -> c -> a`); agent_type is
registered and not `planner`; non-empty name and description. Also `topological_order` (ties by
key), `execution_levels` (level N = all deps in earlier levels = parallelisable), `roots`,
`leaves`, `to_mermaid` ("graph TD", nodes `key (agent_type)`, arrows dependency → dependent).
Manual `POST /workflows` with tasks runs the same rules (422 lists all errors); a workflow with
no tasks is allowed (it gets planned later).

### Planner + repair flow (`app/agents/planner.py`, `app/services/planning_service.py`)
- Planner output `Plan{plan_summary, tasks[PlannedTask{key, name, description, agent_type, input,
  depends_on, requires_approval, rationale}]}`. Its system prompt is built at call time from the
  registry (assignable agents, descriptions, output fields, `[SENSITIVE]` flags), the rules, and
  an example parallel plan. The user prompt holds the goal, workflow context and constraints.
- `plan(llm, goal, constraints, context)`: call planner → `validate_plan` → if invalid, send the
  previous plan + exact errors back, up to `PLANNER_MAX_REPAIR_ATTEMPTS` (default 2, so max 3
  calls) → still invalid → `PlanningError` (422, all errors in `details`). Each attempt is logged.
  LLM errors propagate unchanged (no retries; Part 5).
- Sensitive policy (code-enforced): `BaseAgent.sensitive`; `enforce_sensitive_policy` forces
  `requires_approval=True` for sensitive agents' tasks after planning AND on manual creation.
  The LLM may add approvals but can never remove one. `publisher` is sensitive (Part 6).
- `plan_workflow`: allowed only for workflows with 0 tasks that are PENDING, or FAILED (failed
  planning). Row lock → PLANNING + `workflow.planning_started` **committed before the LLM call** →
  success: one transaction stores tasks, edges, `task.created` events, `context.plan_summary`,
  `workflow.plan_created` (summary, task_count, execution_levels, repair_attempts, tokens) and
  PLANNING → PENDING (planned, ready for Part 4). Failure: PLANNING → FAILED, `workflow.error`,
  `workflow.planning_failed` (error_type, errors). Otherwise 409.
- New transitions: PLANNING → PENDING, FAILED → PLANNING (PLANNING → FAILED existed).
- Planning runs synchronously inside the request; Part 4 moves it to the background.

### Thinking level (latency)
`types.ThinkingConfig(thinking_level=...)` (Gemini 3+). `PLANNER_THINKING_LEVEL` (default high)
for the planner, `WORKER_THINKING_LEVEL` (default low) for the other agents; per-agent override via
the `thinking_level` class attribute. Unsupported models (major version < 3 or aliases) ignore it
and log `llm.thinking_level_unsupported` once (client singleton).

### Endpoints
- `POST /workflows` — `auto_plan: true` (+ optional `constraints`) with no tasks plans in the same
  request; planning failure still returns 201 with status FAILED and `error`.
- `POST /workflows/{id}/plan` — body `{constraints?: {max_tasks?, notes?}}`; 409 if not allowed,
  422 PlanningError, 503/502 LLM errors (workflow is left FAILED and re-plannable).
- `POST /plans/preview` — `{goal, context?, constraints?}` → plan, execution_levels, mermaid,
  repair_attempts, forced_approvals. Stores nothing.
- `GET /workflows/{id}/graph` — nodes (key, name, agent_type, status, requires_approval),
  edges `{from, to}`, execution_levels, mermaid.

## Execution engine (Part 4)
**Deterministic code runs the plan. The DB is the single source of truth: every tick re-reads
state, so nothing depends on engine memory (this is what makes Part 5 crash recovery possible).**

### Architecture (`app/engine/`)
`Engine` is started/stopped in the FastAPI lifespan (`app.state.engine`; None when
`ENGINE_ENABLED=false`). Its loop calls `tick()` every `ENGINE_POLL_INTERVAL_SECONDS`, or at once
when `wake()` is called (task finished, workflow started/created with run=true). Each step below
is its own short transaction:
1. `pickup_run_requests` — workflows with `run_requested` and PENDING, claimed `FOR UPDATE SKIP
   LOCKED`: with tasks → started; without → moved to PLANNING in the same transaction and a
   background job (`runner_jobs.plan_and_start`) plans then starts it. Never planned twice.
2. `schedule_workflow` per RUNNING workflow (row locked `SKIP LOCKED`):
   any task FAILED → workflow FAILED (`workflow.failed`, error names the task);
   all SUCCEEDED/SKIPPED → COMPLETED with `result = {final_outputs: {leaf_key: output}, summary}`
   (`workflow.completed`: duration_seconds, task_count, tokens);
   otherwise promote PENDING whose deps all SUCCEEDED → READY (`task.ready`);
   approval gate: see Part 6 (non-freezing; claim-time approval check).
3. `claim_tasks` — up to free slots (`ENGINE_MAX_CONCURRENCY` − in flight) READY tasks of RUNNING
   workflows, `FOR UPDATE OF tasks SKIP LOCKED`, ordered by workflow started_at then task
   created_at; same transaction: RUNNING, `attempt_count += 1`, `started_at` (first time),
   `lease_owner = ENGINE_WORKER_ID`, `lease_expires_at = now + TASK_TIMEOUT_SECONDS`
   (`task.started` with attempt). Heartbeats / lease expiry handling: Part 5.
4. Dispatch: each claimed task runs `worker.execute_task` in its own asyncio task, bounded by an
   `asyncio.Semaphore`; tracked in `_inflight`; a done-callback logs crashes and wakes the loop.

### Worker (`worker.py`)
Reads task + DIRECT dependency outputs from the DB (`context.build_agent_input`), closes the
session, runs the agent under `asyncio.wait_for(TASK_TIMEOUT_SECONDS)` (no transaction open
during the LLM call), then in ONE transaction: re-check the task is still RUNNING and
`lease_owner` is this worker (else discard + warn: the lease guard for Parts 5/6), save output +
`metrics` (model, tokens, latency_ms, sources) → SUCCEEDED (`task.succeeded`), or `error` +
`error_type` → FAILED (`task.failed`), and clear the lease. error_type: `transient` / `permanent`
/ `output` (Part 2 LLM classes), `timeout` (task timeout), `unknown` (anything else, traceback
logged). No retries in Part 4.

### Failure policy
A FAILED task fails the workflow, but downstream tasks stay PENDING (not skipped/cancelled) and
READY siblings are not claimed; tasks already running finish and save their output. Reason:
Part 5 can resume the workflow from exactly the failed task without redoing completed work.

### Shutdown
`Engine.stop()`: stop claiming/picking up, wait up to `ENGINE_SHUTDOWN_GRACE_SECONDS` for in-flight
work, then cancel it. Cancelled tasks stay RUNNING with their lease (planning jobs stay PLANNING)
and are NOT marked failed; Part 5 recovery reclaims them.

### Events
A state change records ONE event; `transition_*(..., event_type=...)` names it (payload always
has from/to): `workflow.started|completed|failed|awaiting_approval`,
`task.ready|started|succeeded|failed|awaiting_approval`. Default stays `*.status_changed`.

### Config
`ENGINE_ENABLED` (true; tests false), `ENGINE_MAX_CONCURRENCY` (4),
`ENGINE_POLL_INTERVAL_SECONDS` (1.0), `TASK_TIMEOUT_SECONDS` (180),
`ENGINE_SHUTDOWN_GRACE_SECONDS` (10), `ENGINE_WORKER_ID` (hostname-pid-random).

### Endpoints
- `POST /workflows` `run: true` → 202 at once (`run_requested`; constraints saved in
  `context.plan_constraints`); the engine plans and runs it. `auto_plan` (sync) still works.
- `POST /workflows/{id}/start` → 202; PENDING with tasks only, else 409 (no tasks / already
  running / terminal).
- `GET /workflows/{id}` adds `progress` (counts per task status) and `duration_seconds`.
- `GET /workflows/{id}/result` → result when COMPLETED, else 409.
- `GET /workflows/{id}/tasks/{key}` → full task (output, metrics, error, error_type, attempts, lease).
- `GET /engine/status` → enabled, running, worker_id, max_concurrency, in-flight ids, last_tick_at.
- `GET /workflows/{id}/graph` node status is live.

### Testing the engine
Tests set `ENGINE_ENABLED=false`; the `make_engine(llm, **settings)` fixture builds an Engine on the
test DB; `await engine.run_until_idle()` ticks until nothing is in flight and nothing changes.
`FakeLLMClient(delay=, delays={OutputModel: s}, scripts=[FakeScript(match, error=, hang=,
delay=, response=)])` scripts per-task behaviour (match on `"## YOUR TASK
Task <key>"`).

## Reliability (Part 5)
**Every save is conditional, every RUNNING task has a lease, and a restart reclaims orphans from
the DB. Completed work is never redone.**

### Model fallback + circuit breaker (`app/llm/gemini_client.py`, `app/llm/circuit.py`)
Chain is `[GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]`. Next model only on overloaded (503) or
model-not-found (404); quota/permanent/output errors are raised as-is (a quota-exhausted primary
does NOT fall back, by spec). Per-model in-memory breaker: opens after
`MODEL_CIRCUIT_FAILURE_THRESHOLD` overloads, half-open trial after `MODEL_CIRCUIT_COOLDOWN_SECONDS`.
All open → `LLMOverloadedError` with retry_after = soonest reopening. `LLMResponse.model` and
`task.metrics.model` record the model actually used (`llm.used_fallback` WARNING).

### Retry policy (`app/engine/retry.py`)
`fail_or_retry`: retryable type and `attempt_count < max_attempts` → RETRYING with
`next_attempt_at = now + delay` (`task.retry_scheduled`); else FAILED (`task.failed`).
delay = min(MAX, BASE·2^(attempt−1)); with jitter: uniform(0, delay) (full jitter); then
max(delay, retry_after) capped at MAX. The scheduler promotes due RETRYING → READY
(`task.ready` {retry: true}); the engine loop sleeps at most until `next_retry_due()`.
Every claim opens a `task_attempts` row (attempt_number continues across manual retries); it is
closed as succeeded / failed / worker_lost with error_type, model, tokens, latency.

### Leases, heartbeats, idempotency
Claim sets `lease_expires_at = now + LEASE_DURATION_SECONDS`. The worker heartbeats every
`HEARTBEAT_INTERVAL_SECONDS` with a conditional UPDATE (id, RUNNING, lease_owner, attempt_count);
0 rows → lease lost → the agent call is cancelled and its result discarded. The final save uses
the same guard, so a stale worker can never overwrite a newer attempt. `reap_expired_leases`
(each tick) → `task.lease_expired` + `worker_lost` → retry policy.

### Startup recovery (`app/engine/recovery.py`, before the first tick)
`ENGINE_SINGLE_NODE=true`: every RUNNING task is an orphan of a dead process → reclaimed at once
(worker_lost → RETRYING). false: wait for the reaper (lease expiry). PLANNING workflows →
PENDING + run_requested (`workflow.planning_recovered`) so they are re-planned. One
`engine.recovered` event per affected workflow (reclaimed_task_keys, replanned, worker_id).
Never run two single-node engines on the same DB.

### Manual retry
`POST /workflows/{id}/retry` → 202, FAILED only (else 409). With tasks: FAILED tasks → PENDING
(attempt_count 0), workflow → RUNNING, succeeded tasks untouched (`workflow.retried`
{reset_tasks}). Without tasks (planning failed): run_requested → re-planned.
`GET /workflows/{id}/tasks/{key}/attempts` lists attempt rows.

### Fake provider + chaos (dev only)
`LLM_PROVIDER=fake` → `app/llm/demo_fake.py` canned 5-task plan and outputs with
`FAKE_LLM_DELAY_SECONDS` per call (big WARNING banner at startup). `/dev/chaos` POST/GET/DELETE
(404 when APP_ENV=production; `chaos.inject` is inert there too): rule {workflow_id, task_key,
mode, times}; modes crash_process (os._exit(1) after `CHAOS_CRASH_DELAY_SECONDS`),
raise_transient, raise_overloaded, raise_permanent, hang. `remaining` is decremented and committed
before the fault fires, so a crash never repeats forever.

### Crash demo
`python scripts/crash_demo.py --fake` (or without `--fake` for Gemini): starts its own uvicorn on
port 8765 (logs in `logs/`), runs a workflow, arms crash_process on the writer, shows the crash,
reads the DB directly, restarts, prints `engine.recovered`, waits for COMPLETED, prints per-task
attempts and PASS/FAIL (PASS = completed tasks ran once, the orphan has worker_lost then succeeded).
Stop any other server on the same DB first.

### Config
`LLM_PROVIDER` (gemini), `FAKE_LLM_DELAY_SECONDS` (3), `GEMINI_FALLBACK_MODELS` (empty;
.env.example suggests gemini-3.5-flash-lite,gemini-3.1-flash-lite), `MODEL_CIRCUIT_*` (3, 120),
`RETRY_BASE_DELAY_SECONDS` (2), `RETRY_MAX_DELAY_SECONDS` (60), `RETRY_JITTER` (true),
`DEFAULT_TASK_MAX_ATTEMPTS` (3), `LEASE_DURATION_SECONDS` (30), `HEARTBEAT_INTERVAL_SECONDS` (10),
`ENGINE_SINGLE_NODE` (true), `CHAOS_CRASH_DELAY_SECONDS` (2). Migration 0003: `tasks.next_attempt_at`,
`task_attempts`, `chaos_rules`.

## Human controls (Part 6)
**A human can steer any workflow while it runs, and a sensitive step never runs without an explicit
approval. Every action is one locked transaction, recorded as an event with `actor` (+ `reason`),
safe to repeat, and survives restarts (it is all in the DB).**

### Control endpoints (`app/services/control_service.py`, `app/api/routes_workflows.py`)
| Endpoint | Body | Allowed from | Repeat / otherwise |
|---|---|---|---|
| `POST /workflows/{id}/pause` | `actor?, reason?, mode: graceful\|immediate` | RUNNING, AWAITING_APPROVAL | PAUSED → 200 unchanged; else 409 |
| `POST /workflows/{id}/resume` | `actor?` | PAUSED | 409 |
| `POST /workflows/{id}/cancel` | `actor?, reason?` | PENDING, PLANNING, RUNNING, PAUSED, AWAITING_APPROVAL, FAILED | CANCELLED → 200 unchanged; COMPLETED → 409 |
| `POST /workflows/{id}/retry` (P5) | – | FAILED | 409 |
`actor` defaults to "admin". All are 200 (retry 202) and return the workflow detail.
- **PAUSED** is never scheduled (no promote / gate / claim); the reaper still runs; RETRYING tasks keep
  `next_attempt_at`. A paused workflow stays paused across restarts (recovery may reclaim its RUNNING
  tasks to RETRYING, but nothing is promoted until resume).
- **graceful**: in-flight tasks finish and save normally. **immediate**: RUNNING tasks → READY
  (`task.interrupted`), attempt row outcome `interrupted`, lease cleared, local worker cancelled
  (`Engine.interrupt`). `attempt_count` is NOT decremented (it is the fencing token of the ownership
  guard); `max_attempts += 1` instead, so no retry is used up. Workers in other processes see the
  status change on their next heartbeat/save (guard: status RUNNING + lease_owner + attempt_count)
  and discard their result.
- **resume** → RUNNING, or AWAITING_APPROVAL when only pending approvals block it
  (`scheduler.can_progress`). SUCCEEDED tasks never re-run.
- **cancel** (one transaction): workflow CANCELLED (`error = "cancelled by <actor>: <reason>"`,
  run_requested=false); PENDING/READY/RETRYING/AWAITING_APPROVAL/RUNNING tasks → CANCELLED
  (`task.cancelled`; running attempts closed `cancelled`, results discarded); pending approvals →
  cancelled; `workflow.cancelled` {actor, reason, cancelled_tasks, running_tasks_stopped,
  cancelled_approvals}. SUCCEEDED outputs stay. During PLANNING: `complete_planning` re-locks the row
  after the LLM call and discards the plan if it is no longer PLANNING (no tasks inserted).

### Approval lifecycle (`app/services/approval_service.py`, table `approvals`)
```
scheduler: READY task, requires_approval, no approved row
      -> task AWAITING_APPROVAL + approval "pending"  (approval.requested; webhook after commit)
pending --approve(actor, comment?, input_overrides?)--> approved  task READY (approval.approved)
pending --reject(actor, reason, fail_workflow)-------> rejected  task FAILED 'rejected', workflow FAILED
pending --reject(actor, reason, skip_task)-----------> rejected  task + all descendants SKIPPED
pending --expires_at passed (scheduler tick)---------> expired   task FAILED 'approval_expired', workflow FAILED
pending --workflow cancelled-------------------------> cancelled
```
- Partial unique index: at most one pending approval per task. Decisions lock workflow → approval →
  task (`FOR UPDATE`): of two simultaneous decisions exactly one wins, the other gets 409.
  Approve/reject only while pending and unexpired (409 otherwise).
- `request_payload`: workflow_goal, task key/name/description, agent_type, why_sensitive, exact
  task_input, `dependency_previews` (JSON, truncated to 1500 chars each).
- `input_overrides` are shallow-merged into `task.input` and stored on the approval; the event
  records `input_changes` {key: {from, to}}. Approving a PAUSED workflow keeps it PAUSED.
- **Non-freezing gate**: a pending approval only blocks its own branch. The workflow is
  AWAITING_APPROVAL only when nothing can run (no READY/RUNNING/due RETRYING/promotable task) and an
  approval is pending; it goes back to RUNNING as soon as something can run.
- **Claim-time check**: `claim_tasks` only claims a requires_approval task if an `approved` row
  exists (never trusts the status alone). An approval stays valid for automatic retries and crash
  recovery; a manual `/retry` of a REJECTED (or expired) task requests a new approval.
- skip_task: `workflow.result.skipped` lists the skipped keys; the workflow can still COMPLETE.
- Recovery never touches AWAITING_APPROVAL tasks; pending approvals survive restarts.
- Endpoints: `GET /approvals?status=&workflow_id=` (oldest first), `GET /approvals/{id}`,
  `POST /approvals/{id}/approve` {actor, comment?, input_overrides?},
  `POST /approvals/{id}/reject` {actor, reason, on_reject: fail_workflow|skip_task}.
- New error_types (not retryable): `rejected`, `approval_expired`.

### Publisher (`app/agents/publisher.py`, table `publications`)
Sensitive (always needs approval), deterministic, NO LLM call. Publishes the writer's title +
content (approval overrides `title`/`content`/`note` win) to `PUBLISH_DIR/<workflow_id>__<task_key>.md`
and inserts a `publications` row. Idempotent with `idempotency_key = "<workflow_id>:<task_key>"`
(UNIQUE): existing row → return it (`already_published: true`); else write the file atomically
(temp + `os.replace`, deterministic bytes) THEN `INSERT ... ON CONFLICT DO NOTHING`. A crash between
the two leaves a file without a row; the retry rewrites the same file and inserts once. The worker
gives agents `session_factory` (only side-effect agents use it). Planner rule 10: publisher only when
the goal asks to publish/post/share, always last, after the reviewer. The fake planner adds
`publish_report` when the goal contains "publish". `POST /agents/publisher/run` → 502 (needs a workflow).

### Auth (`app/api/auth.py`)
`ADMIN_API_KEY` (SecretStr): required when APP_ENV=production (settings refuse to load without it);
unset in development = open. When set, pause/resume/cancel/retry/approve/reject need header
`X-API-Key` (constant-time compare, 401 otherwise). Reads, create and start stay open. Never logged.

### Visibility
`GET /workflows/{id}` adds `pending_approvals {count, ids}` and `controls {can_pause, can_resume,
can_cancel, can_retry}` from `state_machine.allowed_controls()` (derived from the transition table).
New events: `workflow.paused|resumed|cancelled`, `task.interrupted|cancelled|skipped`,
`approval.requested|approved|rejected|expired`.

### Demo
`python scripts/controls_demo.py` (fake LLM, own server on 8765, admin key "demo-admin-key"):
A pause/resume + approve with an input override (file published once, retry → 409, 1 row),
B reject skip_task (COMPLETED, nothing published), C cancel while running (results discarded).

### Config
`ADMIN_API_KEY` (unset), `APPROVAL_TIMEOUT_HOURS` (24; 0 = never), `APPROVAL_WEBHOOK_URL` (unset;
best-effort POST of the request payload, 5 s timeout, failures only logged),
`PUBLISH_DIR` (outputs/published, gitignored). `ENABLE_SEARCH_GROUNDING` default is now false.
Migration 0004: `approvals`, `publications`.

## Observability & demo (Part 7)
- `GET /workflows/{id}/timeline` (`app/services/timeline_service.py`): built only from DB rows, so it
  tells the same story after a restart. Returns `summary` (tasks, attempts, attempt_outcomes,
  retries_scheduled, restarts_recovered, approvals by status, tokens, models), `spans` (per task:
  status, depends_on, attempts with start/end offsets, outcome, worker, error_type, model) and
  `entries` (one readable `text` per event via `describe()`, offsets in seconds since creation).
  When you add a new event type, add a line to `describe()` (unknown types fall back to from -> to).
- Dashboard: `GET /dashboard` serves `app/static/dashboard.html`, one dependency-free page (vanilla
  JS, no build) that only uses the public API: workflow list, create (run=true), progress, controls
  enabled from `controls`, approval cards (comment, JSON input overrides, approve / reject skip /
  reject fail), attempt bars per task, timeline, result. Polls every 2s; `#<workflow_id>` deep-links;
  the X-API-Key and "You" (actor) fields are kept in localStorage. All rendered strings are
  HTML-escaped (goals and outputs are untrusted). Light/dark via prefers-color-scheme.
- `crash_demo.py` ends by printing the timeline. No separate CLI: `run_workflow.py` and the two
  demos cover terminal use.
- README is the user-facing guide (quick start with LLM_PROVIDER=fake, demos, API, config).

## Live web research + Quick Ask (v1 extras)
- `app/search/web.py`: `gather(queries, settings) -> WebContext` searches each query, de-duplicates
  URLs, reads the top pages in parallel (httpx, per-page timeout, trafilatura main text from raw
  bytes so charsets decode right) and returns numbered `WebSource`s. Never raises. Provider order with
  WEB_SEARCH_PROVIDER=auto: Tavily (TAVILY_API_KEY) else ddgs (no key; engines from
  WEB_SEARCH_BACKENDS tried one by one). `none` disables it (tests set this).
- Researcher: uses the free layer unless ENABLE_SEARCH_GROUNDING (Gemini billing) is on. Queries come
  from the planner (`input.search_queries`, rule 8) or the task/goal. Prompt gets a "WEB SOURCES
  (retrieved <date>)" block; the model must cite [n]; postprocess sets `sources` to the pages actually
  cited (code decides, hallucinated URLs dropped); no pages -> gap note + confidence low.
- `prompts.today()`: every agent prompt (and the planner) gets `## TODAY` so "latest" means now.
- Models: `LLMClient.generate_structured(..., model=None)`. Workers pass GEMINI_WORKER_MODEL
  (`BaseAgent.resolve_model`; planner returns None = GEMINI_MODEL). The Gemini chain for a preferred
  model is [model, other fallbacks, primary last].
- Quick Ask: `POST /ask {question}` -> `ask_service.ask`: search + read + one worker-model call ->
  {answer, key_points, confidence, follow_up_questions, sources[n,title,url,domain,cited], model,
  search_provider, timings_ms, warnings}. Stateless (no DB rows). Dashboard: Ask panel on top;
  [n] / [1, 4] citations link to sources.

## Multi-provider AI + cost control (Part 8)
**Code chooses the provider, falls back across providers, prices every call, enforces budgets and
quotas. The LLM never decides any of it. Only GEMINI_API_KEY + no models.yaml = v1 behaviour.**

### Providers (`app/llm/providers/`)
Each adapter makes ONE call to ONE model and maps errors into the taxonomy; no fallback inside.
- `gemini.py`: the v1 per-model Gemini logic moved unchanged (`GeminiProvider`); `gemini_client.py`
  keeps the legacy `GeminiClient` (fallback loop) as a wrapper for old imports/tests.
- `anthropic_provider.py`: `messages.parse(output_format=Model)` (native structured output, anthropic
  2.14), thinking_level -> `output_config.effort` (low/medium/high); refusal -> permanent;
  `OverloadedError` -> overloaded; spend-limit classes -> quota_exhausted; 429 -> transient + retry-after.
- `openai_provider.py` (`OpenAICompatProvider`): `chat.completions.parse(response_format=Model)` (strict
  JSON schema, openai 3.59), thinking -> `reasoning_effort`, dropped per model after a 400 ("reasoning_effort"
  or Ollama's "does not support thinking"). `insufficient_quota` -> quota_exhausted.
- `ollama_provider.py`: the same adapter on Ollama's OpenAI-compatible `/v1` (OLLAMA_BASE_URL). Chosen
  because it honours `response_format` JSON schemas on Ollama 0.40 (verified live) and shares one
  adapter with OpenAI. Registered only if `GET /v1/models` answers within 2 s at startup.
- `fake_provider.py`: LLM_PROVIDER=fake behind the router (usage rows + budgets work in demos).
- `base.py`: `status_error()` HTTP->taxonomy table, `parse_json`, `repair_prompt` (one repair each).
- `registry.build_providers`: a provider without a key is not registered (one INFO line, no key logged).
- Error taxonomy additions: `LLMAuthError` (401/403/bad key; permanent, never falls back, ERROR log),
  `LLMTimeoutError` (error_type `timeout`, falls back), `BudgetPause` (control signal, not an error).

### Routing (`app/llm/routes.py`, `app/llm/router.py`, `config/models.yaml`)
- `routes: {default: [...], planner|researcher|analyst|writer|reviewer|ask: [...]}`, entries
  `provider:model` (first `:` splits, so `ollama:qwen3:4b` works); `quotas: {entry: {requests_per_day,
  requests_per_minute, timezone}}`. Validated at startup (`RouteConfigError`: bad YAML, unknown provider,
  bad entry, empty route, missing default, no usable entry for a role). Unconfigured providers' entries
  are skipped with a WARNING. No file -> `legacy_routes` (GEMINI_MODEL + fallbacks; worker roles start with
  GEMINI_WORKER_MODEL), unfiltered so a keyless setup fails at call time exactly like v1.
- `get_llm_client()` returns a lazily started `ModelRouter`; the lifespan calls `ensure_started()` so a bad
  models.yaml stops the app. Agents pass `role=self.agent_type` (Quick Ask: `ask`).
- Router per call: skip open circuits (breaker keyed by provider:model) -> budget pre-checks -> atomic quota
  reserve -> adapter. Fallback on overloaded, quota_exhausted (marks the window exhausted), model-not-found,
  timeout; stop on output/auth/plain transient (task retry policy handles transient). Every real call ->
  one `llm_calls` row. All fail -> overloaded if any overloaded, else the last error. Response carries
  provider, model, cost_usd, route_position, fallback_from (= the route's first entry).
- `scripts/list_provider_models.py` lists `provider:model` for every configured provider (free).
  `config/models.example.yaml`: (a) gemini-only, (b) free dev (Ollama first), (c) best quality
  (cross-provider; Anthropic/OpenAI lines are placeholders without keys) + the recommended active profile.

### Cost, budgets, quotas
- `config/pricing.yaml` -> `Pricing.cost(entry, in, out)` per 1M tokens; ollama/fake = 0; unknown -> None + one
  WARNING. Cost lands in llm_calls, task metrics (`provider, cost_usd, fallback_from, route_position`) and
  `task_attempts` (`provider`, `cost_usd`).
- Totals = SQL sums over `llm_calls` per workflow (single source of truth incl. planner, repairs, failed
  calls; no counters to drift). `budget_service`: `effective_budget` (workflow.budget over DEFAULT_MAX_*,
  explicit null = unlimited), `get_usage`, `exceeded` (>=; order calls, tokens, cost, duration), `remaining`.
- A reached limit PAUSES (never fails): `pause_locked` = graceful pause (actor "budget", reason
  "budget_exceeded: <limit>") + `workflow.budget_exceeded` {limit, estimated, usage, budget}. Where: the
  scheduler (before promote/claim, incl. max_duration), the router after every call, and before a task's
  call (already reached, or the pre-call estimate: prompt chars/4 + the role's average output tokens or
  DEFAULT_ESTIMATED_OUTPUT_TOKENS, priced, would cross max_cost_usd) -> `BudgetPause` -> the worker returns
  the task to READY (attempt outcome `budget_paused`, max_attempts += 1, no retry used). Planner calls are
  counted but never blocked (a PLANNING workflow can't pause; the scheduler pauses it once RUNNING).
- `PATCH /workflows/{id}/budget {actor, budget}` merges limits, event `workflow.budget_updated` {actor,
  from, to}; then resume as usual. `POST /workflows` accepts `budget`; detail adds `effective_budget`,
  `usage`, `remaining`.
- `llm/quota.py`: `provider_usage` rows per (provider, model, day|minute, window_start); `try_reserve` is one
  `INSERT ... ON CONFLICT DO UPDATE ... WHERE count < limit AND NOT exhausted RETURNING` (exact under
  concurrent engines); day windows start at local midnight of the entry timezone (QUOTA_TIMEZONE default
  UTC; Google = America/Los_Angeles); `mark_exhausted` on a provider quota error; blocked windows logged once.

### API, dashboard, timeline
`GET /providers` (configured, key_present only, entries with circuit + today's usage vs quota, routes),
`GET /metrics/summary` (totals incl. unpriced calls and fallbacks; per provider and per entry: calls,
errors, error_rate, avg latency, cost). Timeline summary adds llm_calls, fallbacks, cost_by_provider
(null when a success was unpriced), cost_usd; spans carry fallback_from and per-attempt provider/cost;
`describe()` covers budget events and "via provider:model, fallback from X". Dashboard redesigned
("reference desk": oak bar, manila index cards with a catalog-red rule, typewriter face for data only,
self-hosted fonts under /static/fonts): Quick Ask first with "see also" source tracings and `?q=` deep
link; budget meters, budget-paused banner with an adjust form; Providers panel; provider:model and
fallbacks per task/attempt; regions refresh without losing focus or scroll. Design record:
PRODUCT.md, `.impeccable/surfaces/app-static-dashboard-html.md`.

### Review hardening (final review, all with regression tests in tests/test_review_fixes.py)
- A workflow is never paused by its budget once nothing else is left to run: the scheduler applies
  fail/complete before the budget check, and the router's after-call pause skips the last task.
- Every entry out of quota -> `quota_exhausted` (not retried), never "overloaded". Legacy routes (no
  models.yaml) keep v1 rules: fall back only on overload/404, quota windows in America/Los_Angeles.
- Loop order: circuit-open skip -> budget pre-checks -> adapter present? -> quota reserve -> breaker.allow()
  -> call. The breaker is always settled (any provider answer = success; overload/timeout = failure;
  cancellation releases a half-open trial) and a cancelled call still writes an `llm_calls` row.
- Unreachable provider (connection refused, e.g. Ollama stopped) = overloaded -> falls back.
- `max_cost_usd` + an unpriced paid entry -> pause before the call (`budget.unpriced_entry` warning).
- HTTP 400/402/429 with billing/credit/quota wording (Anthropic "credit balance is too low") -> quota_exhausted.
- Startup WARNING `router.timeout_budget` when TASK_TIMEOUT_SECONDS can't fit the first two entries' timeouts.
- Never run two pytest sessions at once: they share agentflow_test and the schema rebuild deadlocks them.

### Live findings (2026-11-26)
- Ollama 0.40.18 installed (winget), models `llama3.2:3b` (2.0 GB, ~5-10 s per call on the RTX 3050 6 GB)
  and `qwen3:4b` (2.5 GB; thinks by default: 70 s with effort low, >120 s timeout without it).
- llama3.2:3b is NOT a reliable planner: it returned a 4-task plan three times for max_tasks=3 (rejected by
  validation). Route the planner to Gemini (or qwen3:4b when offline). Workers on llama3.2:3b work.
- Free metasearch + Quick Ask verified live; Gemini Google-Search grounding still needs billing.

### Demos
`python scripts/budget_demo.py` (fake): max_llm_calls=3 -> PAUSED at 3 calls -> PATCH 10 -> resume ->
COMPLETED at 6 calls, $0. crash_demo --fake and controls_demo still PASS.

### Config
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (optional), `OLLAMA_BASE_URL` (http://localhost:11434/v1),
`LLM_TIMEOUT_SECONDS` (120), `MODELS_CONFIG_PATH` (config/models.yaml), `PRICING_CONFIG_PATH`
(config/pricing.yaml), `QUOTA_TIMEZONE` (UTC), `DEFAULT_MAX_COST_USD|TOKENS|DURATION_SECONDS` (none),
`DEFAULT_MAX_LLM_CALLS` (50), `DEFAULT_ESTIMATED_OUTPUT_TOKENS` (1500). Migration 0005: `llm_calls`,
`provider_usage`, `workflows.budget`, `task_attempts.provider|cost_usd`. Live markers: `live_gemini`,
`live_anthropic`, `live_openai`, `live_ollama` (all also `live`, excluded by default).

## Tools + safe actions (Part 9)
**The LLM proposes a tool call; code validates, authorizes, executes, journals it and decides whether a human
must approve. No `config/tools.yaml` = no agent has tools = Part 8 behaviour exactly.**

### Framework (`app/tools/`)
- `base.py`: `Tool` ABC (name, description, input_model, output_model, side_effect `none|internal_write|
  external_read|external_write` (`side_effect_for(args)`: http_request GET = read), untrusted, idempotent,
  supports_idempotency_key / `replay_safe(options)`, timeout_seconds, `available()` = disabled reason),
  `ToolContext` (workflow/task/attempt, sandbox, idempotency_key, options = the tools.yaml entry, credentials,
  `approval_flags` set ONLY by an approver, e.g. allow_recipient_override), `ToolError(transient=)`.
- `registry.py` + `catalog.py` (imports every tool module), `config.py`: tools.yaml (`TOOLS_CONFIG_PATH`)
  `{tools: {name: {enabled, approval: never|always|external_write_only, on_reject: inform_agent|fail_task,
  ...options}}, agents: {agent: [tools]}, credentials: {name: {env, header, format}}}` validated at startup
  (`ToolConfigError` stops the app). `effective_tools` = agent `allowed_tools` ∩ config ∩ enabled ∩ available.
  `approval_mode` default: always for external_write. `CredentialResolver`: the agent passes a NAME; secrets
  come from env and are redacted (`redact.py`) from journal rows, results and logs. `GET /tools`.
- Tools: web_search (backend chain, default [tavily, ddgs]; gemini_grounding opt-in; fake), web_fetch
  (trafilatura -> BeautifulSoup; fake backend with canned pages incl. `https://evil.example/ev-report`),
  http_request (domain allowlist, named credentials), sql_query (sqlglot: one SELECT, write/admin nodes and
  file/admin functions rejected; READ ONLY transaction, statement_timeout, role `agentflow_reader`, row
  limit 200; refuses AgentFlow's own DB), calculator (AST whitelist incl. pct_change), file_write/read/list
  (sandbox `outputs/workspaces/<wf>/`, no traversal/absolute/symlink), send_email (outbox `<key>.eml`
  deterministic | smtp; recipient allowlist; smtp is NOT replay-safe), webhook_post (allowlisted URL prefix,
  `Idempotency-Key` header).
- `netguard.py` SSRF guard (web_fetch, http_request, webhook): http/https only; resolve DNS, EVERY address
  must be global (blocks private, loopback, link-local/169.254.169.254, multicast, reserved, IPv4-mapped
  IPv6, fc00::/7); connects to the vetted IP (Host header + SNI keep the name); every redirect hop re-checked
  (max 5); 2 MB cap while streaming; content-type allowlist. Tests patch `netguard.resolve` and
  `netguard.transport_factory`.
- Sample data: `scripts/setup_sample_data.py` (idempotent; also run by the test session fixture) creates DB
  `agentflow_sample`, table `ev_sales` (FAKE numbers, labelled) and the read-only role (SAMPLE_DATA_URL).

### Tool loop (`app/tools/loop.py`, `prompting.py`)
- `BaseAgent.run`: no effective tools -> the classic single call; else `run_tool_loop`. The worker builds a
  `ToolRuntime` (running attempt number, upstream taint, resume info) and uses `TOOL_LOOP_TIMEOUT_SECONDS`
  (900) instead of TASK_TIMEOUT for tool agents.
- Step model `ToolStep<Output>` = {action tool_call|final, tool, arguments (free object), reason, output};
  every step goes through the router (cost/quota/budget/fallback). Code validates tool + arguments; an
  invalid call is a `rejected_invalid` journal row returned to the model as TOOL ERROR (no crash).
  `MAX_TOOL_STEPS` (8) -> one forced "final now" call -> still no final = LLMOutputError.
- Budget `max_tool_calls` (executed calls; reuses don't count) -> BudgetPause. A tool workflow with no explicit
  budget that hits DEFAULT_MAX_LLM_CALLS pauses with "budget_exceeded: default max_llm_calls=N reached (tool
  workflow); raise the budget with PATCH /workflows/{id}/budget and resume".

### Journal + replay (`journal.py`, table `tool_calls`, migration 0006)
- A row before execution (running), updated after, short transactions. `idempotency_key` = sha256(workflow|
  task_key|tool|args_hash) on the arguments that EXECUTE (after approval edits).
- Resume (user decision): a new attempt after `worker_lost | interrupted | budget_paused | suspended` rebuilds
  the transcript from the previous attempts' rows and asks the model only for the NEXT step.
- Decisions: write succeeded -> reuse (any age); read succeeded -> reuse within TOOL_RESULT_REUSE_SECONDS
  (3600); running/unknown write -> `unknown_outcome`, re-execute with the same key if replay-safe, else a
  human approval with "may have sent this". `GET /workflows/{id}/tasks/{key}/tool-calls`.

### Tool-call approvals
- Needed when approval mode says so, or (code-enforced) the attempt is tainted and the call is
  external_write, or an unknown smtp outcome. `ToolApprovalNeeded` -> worker `_suspend`: attempt `suspended`
  with `loop_state`, lease cleared, max_attempts += 1, task RUNNING -> AWAITING_APPROVAL (new transition),
  approval `kind=tool_call` + `tool_call_id`; the slot is free; waiting time counts toward no timeout.
- Approve: `arguments_overrides` validated against the tool (422, stays pending), `allow_recipient_override`;
  the row gets final args + key (`proposed_arguments` keeps the original). Reject: `inform_agent` (task READY,
  the model sees "denied by human: <reason>") | `fail_task`; task kinds keep fail_workflow | skip_task; wrong
  kind -> 422; body on_reject optional (defaults per kind / tools.yaml). Expiry, cancel -> row `denied`.
  The claim-time approval check only counts `kind=task` rows.

### Taint, injection, sources
- Results of untrusted tools are wrapped `<untrusted_tool_result source=...>` + UNTRUSTED_RULE in every tool
  agent's system prompt. Reading one taints the attempt; `tasks.tainted` is set on success and also when any
  direct dependency is tainted (flows through tasks without tools).
- `injection.py` heuristics flag (never block): journal row, event `tool.injection_suspected`, approval payload.
- Researcher (tool mode): `postprocess_tools` keeps only cited URLs returned by web_search or fetched in this
  task (`sources.normalize_url`), moves the rest to `removed_unverified_sources`, lowers confidence one level,
  event `task.sources_verified`.

### Agents
researcher [web_search, web_fetch], analyst [sql_query, calculator], writer [file_write], communicator
[send_email, webhook_post] (`requires_tools`: offered to the planner only when its tools are configured;
deliveries rebuilt from the journal). Planner catalog lists tools; rule 12 (communicator only when the goal
asks to send/email/notify, last). Demo mode: `FakeLLMClient.tool_policy` = `demo_fake.tool_step`; an
"email/send/notify" goal plans research ‖ analysis -> summary -> email.

### Visibility + demo
Timeline summary: tool_calls_by_tool, tool_calls_reused, tool_approvals, sources_verified/removed,
injection_flags; per-attempt `tool_calls` markers; `/metrics/summary.tools` per tool. Dashboard: tool calls
under each step (reused / untrusted input / suspicious page badges), tool approval card with an editable JSON
of exactly what will be sent. Chaos mode `crash_after_tool:<tool>` (dev only). `python scripts/tools_demo.py`
(fake; `--live` for real providers). docs/DEMO_GUIDE.md has the interview Q&A.

### Config
`TOOLS_CONFIG_PATH` (config/tools.yaml), `MAX_TOOL_STEPS` (8), `MAX_TOOL_OUTPUT_CHARS` (4000),
`TOOL_LOOP_TIMEOUT_SECONDS` (900), `TOOL_RESULT_REUSE_SECONDS` (3600), `WORKSPACES_DIR`, `OUTBOX_DIR`,
`SAMPLE_DATA_URL`, `SAMPLE_DB_READER_PASSWORD`. Migrations 0006 (tool_calls, approvals.kind/tool_call_id,
task_attempts.loop_state, tasks.tainted) and 0007 (asks: Quick Ask history).

### Dashboard redesign (user request, 2026-11-26)
Clean answer engine (DESIGN.md): one question box with Quick answer / Deep research, mic placeholder
("not available yet"), server-side history sidebar (`GET /history`, `GET /asks/{id}`; Quick Ask answers are
saved), technical status removed from the screen (cost/limits/activity in a closed Details section).

## Adaptive workflows (Part 10)
**The plan may change shape while it runs, only within limits that code sets and checks. Finished work is never undone, and the task
graph stays a DAG at every moment.** Spec/plan: `docs/superpowers/specs|plans/2026-11-26-part10-adaptive*`.

### Conditions, branches, joins
- **Condition language** (`app/core/conditions.py`, AST whitelist, never `eval`):
  - Applies to `run_if`, loop `until` / `best_by`, and `replan_if`.
  - Names are task keys exposing `.output` / `.status`; functions len/lower/upper/min/max/abs/round/any/all.
  - Limits: 300 chars, 80 nodes, depth 12, exponent 64, values ≤ 10,000 items (repetition is checked before it
    allocates).
  - Errors: ConditionSyntaxError at plan time; ConditionError (incl. MemoryError/RecursionError) at run time ->
    `condition_error`, never a crash.
  - Scope: run_if = its dependencies; replan_if = itself + dependencies; loop conditions = the loop's body only.
- **Joins** (`app/core/joins.py`): `all_succeeded` (default) | `any_succeeded` | `all_done`.
  - A FAILED task is tolerated when every dependent uses a tolerant join.
  - The workflow then COMPLETES with `result.failed_branches` + event `workflow.completed_with_failed_branches`.
  - `run_if` false -> SKIPPED, `skip_reason=condition_false`.

### Loops = dynamic expansion (`graph_service.expand_loop`, `scheduler._advance_loops`)
- **Plan format:** `loops: [{name, body (contiguous chain), until, max_iterations, on_exhausted:
  continue_with_best|fail|ask_human, best_by?, feedback {from, to}}]`.
  - State lives in `workflow.context.loops` (status open|awaiting_human|expand_pending|choose_best|done|exhausted).
  - Body keys must be ≤ 46 chars (`LOOP_KEY_MAX`; the validator message says why, and the repair loop fixes it).
  - Body tasks may not have `run_if`.
- **Expansion** (`until` false, iteration allowed by max_iterations / max_iterations_total / the max_llm_calls
  pre-check) inserts `<key>__v<n>` rows (base_key, iteration, loop_name):
  - The feedback target gets `revision_feedback`; the writer outputs `changes_made`.
  - Dependents of EVERY body task are rewired to the newest iteration.
  - Expansion is idempotent (crash-safe).
  - Downstream tasks wait while the loop is open (`_blocked_by_open_loop`).
- **Ending a loop:**
  - A dead body task (SKIPPED / CANCELLED / SUPERSEDED) -> `_exhausted` ("loop body did not finish").
  - continue_with_best picks the best FINISHED iteration (best_by error -> latest finished; the reason is in the
    event) and rewires dependents to it.
  - ask_human -> a workflow-level `loop_exhausted` approval (task_id NULL; `extra_iterations` on approve).
- **Results:** `final_outputs` reports the chosen iteration's task for a loop leaf. Lessons go to memory when a loop
  ends after v2+.

### Re-planning (`app/services/replan_service.py`, `app/core/plan_patch.py`)
- **Triggers:**
  - (a) a FAILED task when `context.adaptive.replan_on_failure` (planner plans; default False);
  - (b) `replan_if` true on a succeeded task (checked once);
  - (c) human `POST /workflows/{id}/replan` (any workflow, 202);
  - plus `POST /workflows/{id}/plan-patch` (PAUSED only, a human's own patch).
- **Flow:**
  - `request` -> REPLANNING (`context.replan.state` requested -> planning -> proposed | applied | failed).
  - The engine tick claims requested jobs; `run_job` asks the planner (patch mode, ≤2 repairs).
- **`validate_patch`** checks the merged graph. It rejects:
  - cancelling started, finished or loop tasks;
  - rewiring a non-FAILED task, or one whose dependents already started (`rewire_dependent_started`);
  - existing keys;
  - more than REPLAN_MAX_NEW_TASKS new tasks.
- **Approval:** a `plan_patch` approval (task_id NULL) is needed when the patch adds a sensitive step, adds more than
  REPLAN_AUTO_APPROVE_MAX_TASKS tasks, or replaces a sensitive task.
- **`apply`:** one transaction (new tasks, SUPERSEDED, rewired dependents, `plan_versions` applied), then back to the
  previous status.
- **Failure and expiry:** `_fail` -> automatic trigger -> FAILED; human trigger -> back to before. A plan_patch
  expiry goes through decide_patch/_fail and is checked for REPLANNING workflows each tick
  (`scheduler.expire_replanning_approvals`).
- **Limits and recovery:** MAX_REPLANS (budget `max_replans`). Recovery: planning|applying -> requested.
- **Endpoints:** `GET /workflows/{id}/plan-versions`.

### Memory (`app/services/memory_service.py`, table `memories`, FTS tsvector + GIN)
- **Kinds:** fact (researcher with verified sources, 30 d), lesson (loop, 180 d), artifact_summary (180 d).
- **Flags:** `tainted` / `private` come from the contributing tasks (lessons and summaries too).
- **Retrieval:** `search` = OR tsquery ranked relevance × recency × confidence, pinned first, namespace-scoped
  (`memory: {read, write, namespace}` per workflow).
- **In the prompt:** `context.build_agent_input` adds PRIOR KNOWLEDGE (tainted -> untrusted tags), and
  memory_tainted / memory_private flow into the ToolRuntime. The researcher may answer `from_memory`.
- **API:** `/memories` (GET q/kind/namespace, GET id, pin, DELETE soft, purge-expired).

### Exfiltration guard for reads (`app/tools/exfil.py`)
- **Scope:** a private attempt (SQL rows, private memory) reading a URL with web_fetch / http_request GET.
- **Trusted provenance:** search results, fetched pages, the task input, UNTAINTED upstream outputs, memory sources,
  the read allowlist.
- **Otherwise:** URLs outside that set, too long, or carrying private tokens -> held for a human ("possible data
  exfiltration").
- **http_request GET with a body** = external_write.

### Other Part 10 pieces
- **Approvals:** duplicate-send needs `confirm_duplicate` (409 otherwise); approve bodies are strict (unknown field ->
  422); `POST /approvals/{id}/check`.
- **Logging:** `X-Request-ID` middleware (`rid=` in logs).
- **Dashboard:** plan history, Re-plan button, plan_patch / loop_exhausted cards, skip reasons, failed-branch badge,
  Memory page (`#memory`).
- **Timeline counts:** loop_iterations, replans, condition_skips, failed_branches, memory_hits,
  llm_calls_saved_by_memory.
- **Demo:** `python scripts/adaptive_demo.py` (fake, 12 checks): loop 5 -> v2 -> 9, branches, re-plan SUPERSEDED, crash
  during v2 -> one v2, memory run cheaper, exfil held.
- **Config:** MAX_LOOP_ITERATIONS (5), MAX_REPLANS (2), REPLAN_MAX_NEW_TASKS (5), REPLAN_AUTO_APPROVE_MAX_TASKS (2),
  MEMORY_ENABLED, MEMORY_TOP_K (5), MEMORY_FACT_TTL_DAYS (30), MEMORY_LESSON_TTL_DAYS (180).
- **Migration 0008:** statuses REPLANNING / SUPERSEDED, task loop/branch columns, nullable approvals.task_id,
  plan_versions, memories.
- **Long local test runs:** wrap them in `python scripts/ci/keep_awake.py -- <cmd>`. Windows Modern Standby paused a
  run for 6 h and its 60 s timers then expired.

## Governance: users, teams, roles, policies (Part 11)
**Every caller is a known person with one role in one team. Policy-as-code adds approval rules; a code safety floor
can't be switched off. Teams can't see each other.**

### Identity (`app/api/auth.py`, `app/governance/principal.py`, `app/api/routes_admin.py`)
- **Tables:** `teams`, `users` (name, team_id, role viewer|operator|approver|admin, api_key_hash = sha256, active).
  Migration 0009 also adds `workflows.team_id` + `created_by` and the approval columns `required_approvals`,
  `approver_role`, `votes`, `policy_rules`.
- **`get_principal`** (yield dependency) reads X-API-Key and sets a contextvar (`principal.current()`, reset after the
  request; the engine runs as SYSTEM). Principal kinds:
  - `admin_key`: ADMIN_API_KEY, global admin.
  - `user`: hash lookup, active only.
  - `anonymous`: users exist and no key was sent.
  - `open`: no users; role admin without ADMIN_API_KEY, viewer with it (legacy behaviour).
- **Role gates:** `require_role(viewer|operator|approver|admin)`. `require_admin` = operator (the Part 6 control
  name). Create / plan / start use `require_creator` (open in legacy modes, operator+ with users).
- **Actor:** `Principal.actor(body_actor)`. A user is always recorded as themselves; actor fields are optional now.
- **Admin API:**
  - `/me`;
  - `/admin/teams` (global admin);
  - `/admin/users` (key returned ONCE; a team admin manages only their own team);
  - `/admin/users/{id}/disable|rotate-key`;
  - `/admin/policies`.
  Bootstrap: open mode or the admin key. The first user closes the open mode.

### Tenant isolation (`app/governance/access.py`)
- **Path objects:** router-level `scoped("workflow_id"|"approval_id", check_*)` returns 404 for another team's object.
- **Lists:** `list_workflows` and `list_approvals` filter by `workflow_filters()`.
- **Memory:** a team's namespace = `t<team_id hex[:12]>:<ns>` (set at workflow creation; ≤ 64 chars checked up
  front, 422; memory routes check the prefix).
- **History:** `asks.team_id` / `created_by`; `/history` and `/asks/{id}` are filtered by team.
- **Global principals** (open, admin key, system, a user without a team) see everything. Workflows with NULL team are
  global-only.
- **Gated endpoints:** `/ask`, `/plans/preview` and `/dev/chaos` need operator+ with users (chaos only on your own team's
  workflows). `/metrics/summary` and `/engine/status` are `require_ops_view` (legacy open, else a global admin).
  `/providers` and `/health` stay public.

### Policy engine (`app/governance/policy.py`, `config/policies.yaml` = POLICIES_CONFIG_PATH)
- **Rules:** `{name, when {agent_type, task_key glob, tool, team, side_effect, recipient_not_in_domains},
  require_approval (true only), approvals (1-10 distinct people), approver_role, deny}`, validated at startup
  (`PolicyConfigError`).
- **Evaluation:** `evaluate(scope, **ctx)` merges every matching rule, strictest wins. scope "task" = rules without
  a tool / side_effect / recipient condition (scheduler gate, task approvals); "tool" = only tool rules (tool loop,
  tool approvals). `deny` needs a tool condition (validated).
- **Edited arguments:** an approver's `arguments_overrides` are re-evaluated (`_recheck_edited_call`). deny -> 403;
  stricter -> more approvers / a higher role, earlier votes cleared.
- **Where it applies:**
  - The scheduler gate sets `requires_approval` on READY tasks matched by task rules.
  - `approval_service._apply_policy` stamps `required_approvals` / `approver_role` / `policy_rules` (+ payload.policy)
    on task and tool-call approvals.
  - The tool loop (`ToolLoop._policy`): `deny` -> a rejected_invalid row ("denied by policy (rule)"); `require` ->
    suspend for approval.
- **Multi-approver** (`approval_service._authorize` / `_record_vote`): votes `{voter, actor, at, comment,
  overrides?}`.
  - Not enough votes yet: the approval stays pending (event `approval.vote_recorded`).
  - An edit resets earlier votes. The final apply uses the first vote's overrides.
  - Any eligible reject rejects.
- **SAFETY_FLOOR** (code):
  - sensitive agents always need an approval;
  - a tainted external_write always needs an approval;
  - policies only add requirements;
  - approvers / admins only (a lower approver_role is raised to approver);
  - 2+ approvers: one vote per voter (the admin key = one voter), the workflow creator can't vote (four eyes);
  - one veto rejects.

### Visibility + demo
- **Dashboard:** "Signed in as ..." under Settings (from `/me`; the actor field is locked to the user); approval
  cards show "Needs N different approvers · k so far: names · policy: rules"; 401/403 messages.
- **Timeline:** describes `approval.vote_recorded`.
- **Demo:** `python scripts/governance_demo.py` (fake, 12 checks). Its `gd-*` users/teams are deleted from the DB at the
  start and the end (otherwise every other demo would need keys).

## Coding conventions
- **Async everywhere** (routes, services, DB access). No sync DB calls in app code.
- **Thin routes**: parse/validate → call a service → convert to schema. No logic in routes.
- **Services hold logic and own transactions** (commit on success, rollback on failure).
- **All status changes go through `app/core/state_machine.py`** (`transition_workflow` /
  `transition_task`). Never assign `.status` directly elsewhere. Each transition records a
  `*.status_changed` event with `{"from", "to"}` in the same transaction.
- **All state is persisted in the DB**; no in-memory-only state that matters after a restart.
- Load relationships explicitly with `selectinload` (lazy loading doesn't work under asyncio).
- Errors to clients use `{"error": {"code", "message", "details"?}}`; raise `AppError` subclasses.
- Timestamps are timezone-aware UTC (`app.db.base.utcnow()`).
- Schema changes need a new Alembic migration; `tests/test_migrations.py` fails on drift.
- **Never hardcode or log secrets** (GEMINI_API_KEY is a `SecretStr`; don't log DB URLs).
- Type hints on everything; short docstrings/comments explaining *why*.

## Commands (Windows PowerShell; macOS/Linux equivalents in README)
```powershell
docker compose up -d                       # start Postgres 16
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env                # then edit if needed
alembic upgrade head                       # migrate main DB
alembic revision --autogenerate -m "msg"   # new migration after model changes (review it!)
uvicorn app.main:app --reload              # run API at http://127.0.0.1:8000 (docs at /docs)
pytest -v                                  # default tests: FakeLLMClient, no network, no cost
pytest -m live -s                          # real Gemini calls (needs GEMINI_API_KEY; costs tokens)
python scripts/list_models.py              # models available to your key
python scripts/run_workflow.py "<goal>"    # demo: run a goal end to end (server must be running)
python scripts/crash_demo.py --fake           # Part 5: crash + restart + recovery (fake LLM)
python scripts/controls_demo.py             # Part 6: pause/resume, approve, reject, cancel (fake LLM)
python scripts/budget_demo.py               # Part 8: budget pause + PATCH + resume (fake LLM)
python scripts/setup_sample_data.py         # Part 9: sample database + read-only role for sql_query
python scripts/tools_demo.py                # Part 9: tools + approvals + crash after email + injection (fake)
python scripts/governance_demo.py           # Part 11: roles, isolation, two approvers, four eyes (fake)
python scripts/adaptive_demo.py             # Part 10: loops, branches, re-plan, crash during v2, memory, exfil (fake)
python scripts/demo.py                      # every demo, one PASS/FAIL table
python scripts/list_provider_models.py      # Part 8: models your keys / local Ollama offer
pytest -m live_ollama -s                    # Part 8: free local-model live tests
```
