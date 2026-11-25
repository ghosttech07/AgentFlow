# AgentFlow

A backend engine that turns a goal into finished work with a team of AI agents, and keeps going
when things break.

You submit a goal ("Write a short beginner guide on EV adoption in India and publish it"). A
**planner** agent (Gemini) breaks it into a graph of tasks; **specialist agents** (researcher,
analyst, writer, reviewer, publisher) run them in dependency order, independent tasks in parallel.
Every state change is stored in **PostgreSQL**, so a crash loses nothing: on restart the engine
keeps the finished tasks and re-runs only the one that was interrupted. A human can **pause,
resume or cancel** any workflow, and **sensitive steps wait for explicit approval**.

Research is **live**: the researcher searches the web, reads the top pages and cites every finding
`[n]` (free, no search key needed). For quick questions, **Quick Ask** returns a cited answer from
the live web in seconds, without running a full workflow.

```
goal ──► Planner (LLM) ──► validated task DAG ──► Engine (scheduler + worker pool) ──► result
                             code checks it        every step persisted in Postgres
                                                   retries · leases · crash recovery
                                                   pause/resume/cancel · approvals
```

**Rule of the codebase: AI decides content, code decides control.** Agents only produce content.
Planning is validated by code, and status changes go through one state machine.

## Quick start (no API key needed)

Prerequisites: Python 3.11+ (3.12 recommended) and Docker Desktop.

**Windows (PowerShell)**
```powershell
Copy-Item .env.example .env
docker compose up -d                 # PostgreSQL 16; wait until `docker compose ps` says healthy
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head
$env:LLM_PROVIDER = "fake"           # canned agent output, no Gemini calls
uvicorn app.main:app
```

**macOS / Linux**
```bash
cp .env.example .env
docker compose up -d
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
LLM_PROVIDER=fake uvicorn app.main:app
```

Open **http://127.0.0.1:8000/dashboard**: ask a question at the top (Quick Ask) or type a goal to
run a full workflow. API docs are at `/docs`.
To use real Gemini, put `GEMINI_API_KEY` in `.env` and drop `LLM_PROVIDER=fake`.

## Demos

Each demo starts its own server on port 8765, so stop any other server on the same database first.

| Command | Shows |
|---|---|
| `python scripts/crash_demo.py --fake` | **The headline demo.** Runs a workflow, kills the server (`os._exit`) while the writer runs, reads the DB while it's down (finished tasks SUCCEEDED, writer still RUNNING), restarts, and shows `engine.recovered`. The writer resumes (attempt 1 `worker_lost`, attempt 2 `succeeded`), the workflow completes and nothing finished is redone. Ends with the timeline and **PASS/FAIL**. Drop `--fake` to use Gemini. |
| `python scripts/controls_demo.py` | A: pause (no new tasks for 5s), resume, approve the publish step with an edited title; it is published exactly once. B: reject with `skip_task`; the workflow completes with nothing published. C: cancel while tasks run; in-flight results are discarded. |
| `python scripts/run_workflow.py "<goal>"` | Runs any goal against a running server with a live progress table, then prints the final text. |

## How it works

| Piece | Where | What it does |
|---|---|---|
| Planner | `app/agents/planner.py`, `app/services/planning_service.py` | LLM proposes a plan; `app/core/dag.py` checks keys, deps, cycles and agent types. Invalid plans are sent back with the exact errors (max 2 repairs). |
| Agents | `app/agents/` | researcher, analyst, writer, reviewer (Gemini, structured JSON output) and publisher (no LLM, sensitive). |
| Engine | `app/engine/` | A loop that reads state from the DB on every tick: promote ready tasks, claim them (`FOR UPDATE SKIP LOCKED`), and run them on a bounded worker pool. |
| Reliability | `app/engine/retry.py`, `recovery.py`, `worker.py` | Retries with exponential backoff and full jitter; model fallback with a circuit breaker; leases and heartbeats; a reaper for dead workers; startup recovery; every save is guarded (status + lease owner + attempt) so a stale worker can never overwrite newer work. |
| Human controls | `app/services/control_service.py`, `approval_service.py` | Pause (graceful or immediate), resume, cancel, approval checkpoints with expiry, input overrides and reject modes. |
| Observability | `app/services/timeline_service.py`, `app/static/dashboard.html` | Event log, readable timeline with per-attempt spans, and the dashboard. |

### Failure handling at a glance
- **Transient errors** (timeouts, 429 per minute, 5xx, overloaded) are retried with backoff and
  `retry_after` is respected. **Permanent errors, quota exhaustion and rejections** are not retried.
- **Overloaded model**: the next model in `GEMINI_FALLBACK_MODELS` is tried; the model actually
  used is recorded on the task.
- **Process crash**: on restart, RUNNING tasks are reclaimed as `worker_lost` and retried, and
  interrupted planning is re-planned. SUCCEEDED tasks are never re-run.
- **Side effects**: the publisher is idempotent (`<workflow>:<task>` unique key), so a crash or
  retry never publishes twice.
- **Fault injection (dev only)**: `POST /dev/chaos` can crash the process, raise transient,
  overloaded or permanent errors, or hang a chosen task.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + DB check |
| POST | `/ask` | Quick Ask: `{question}` → live-web answer with `[n]` citations, sources, timings |
| POST | `/workflows` | Create. `run: true` → 202, planned and run in the background. Or pass `tasks` yourself |
| GET | `/workflows`, `/workflows/{id}` | List / detail (tasks, progress, `pending_approvals`, allowed `controls`) |
| POST | `/workflows/{id}/start` | Run a PENDING workflow that has tasks |
| POST | `/workflows/{id}/pause` · `/resume` · `/cancel` · `/retry` 🔑 | Human controls (`actor`, `reason`, pause `mode`) |
| GET | `/workflows/{id}/result` | Final outputs once COMPLETED |
| GET | `/workflows/{id}/timeline` | Readable timeline, attempt spans, summary (attempts, retries, restarts, tokens) |
| GET | `/workflows/{id}/events` · `/graph` · `/tasks/{key}` · `/tasks/{key}/attempts` | Raw event log, DAG + Mermaid, task detail, attempt history |
| GET | `/approvals?status=pending` · `/approvals/{id}` | Approval queue |
| POST | `/approvals/{id}/approve` · `/reject` 🔑 | Decide (`actor`, `comment`, `input_overrides` / `reason`, `on_reject`) |
| POST | `/plans/preview` | Plan a goal without storing anything |
| GET | `/agents`, `/engine/status` | Agent catalog, engine state |
| GET | `/dashboard` | Web dashboard |

🔑 requires the `X-API-Key` header when `ADMIN_API_KEY` is set (always set it in production).
Errors always look like `{"error": {"code": "...", "message": "..."}}`.

```powershell
$h = @{ "X-API-Key" = "my-key" }
$wf = Invoke-RestMethod -Method Post http://127.0.0.1:8000/workflows -ContentType application/json `
      -Body '{"name":"EV guide","goal":"Write a short beginner guide on EV adoption in India and publish it","run":true}'
Invoke-RestMethod "http://127.0.0.1:8000/workflows/$($wf.id)/timeline"
$a = (Invoke-RestMethod "http://127.0.0.1:8000/approvals?status=pending&workflow_id=$($wf.id)")[0]
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/approvals/$($a.id)/approve" -Headers $h `
      -ContentType application/json -Body '{"actor":"me","input_overrides":{"title":"Final title"}}'
```

## Configuration (`.env`)

| Setting | Default | Notes |
|---|---|---|
| `DATABASE_URL`, `TEST_DATABASE_URL` | local Docker Postgres | Tests only ever touch the `agentflow_test` database |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | –, `gemini-3.5-flash` | Free tier: about 20 requests/day on this model |
| `GEMINI_FALLBACK_MODELS` | – | e.g. `gemini-3.5-flash-lite,gemini-3.1-flash-lite` |
| `LLM_PROVIDER` | `gemini` | `fake` = canned output, no network (demos, development) |
| `GEMINI_WORKER_MODEL` | – | Fast model for workers + Quick Ask, e.g. `gemini-3.5-flash-lite` (planner keeps `GEMINI_MODEL`) |
| `WEB_SEARCH_PROVIDER` | `auto` | Live web research: Tavily if `TAVILY_API_KEY` is set, else free metasearch (no key); `none` = off |
| `ENABLE_SEARCH_GROUNDING` | `false` | Gemini's built-in Google Search instead (needs billing on the Google key) |
| `ENGINE_MAX_CONCURRENCY`, `TASK_TIMEOUT_SECONDS` | 4, 180 | Worker pool size, per-task limit |
| `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `DEFAULT_TASK_MAX_ATTEMPTS` | 2, 60, 3 | Backoff policy |
| `LEASE_DURATION_SECONDS`, `HEARTBEAT_INTERVAL_SECONDS`, `ENGINE_SINGLE_NODE` | 30, 10, true | Crash detection |
| `ADMIN_API_KEY` | – | Required when `APP_ENV=production` |
| `APPROVAL_TIMEOUT_HOURS`, `APPROVAL_WEBHOOK_URL` | 24, – | Approval expiry (0 = never), optional notification POST |

The full list with comments is in [.env.example](.env.example).

## Tests

```powershell
pytest            # ~300 tests against the real agentflow_test Postgres; fake LLM, no network, no cost
pytest -m live -s # 4 live tests with real Gemini calls (~10 requests of your daily quota)
```
The suite rebuilds the test schema from the Alembic migrations (downgrade → upgrade), truncates
tables between tests and fails if the models drift from the migrations. Engine tests drive the real
scheduler with a scripted fake LLM (errors, hangs, delays per task), including two engines racing on
the same DB and simulated crashes.

> `agentflow_test` is created by `docker/postgres-init/` only on the volume's first start. If it is
> missing: `docker compose exec postgres createdb -U agentflow agentflow_test`.

## Project layout

```
app/
  agents/     planner + specialist agents, prompts, registry
  api/        routes (workflows, approvals, plans, agents, engine, dashboard, dev chaos), auth
  core/       enums, state machine, DAG rules, events, errors
  engine/     engine loop, scheduler, worker, retry, recovery, chaos
  llm/        Gemini client (fallback + circuit breaker), error classes, fake clients
  models/     SQLAlchemy models    schemas/  Pydantic models    services/  business logic
  static/     dashboard.html
alembic/      migrations 0001-0004
scripts/      crash_demo.py, controls_demo.py, run_workflow.py, list_models.py
tests/        unit, API, engine, reliability, control and live tests
```

[CLAUDE.md](CLAUDE.md) holds the detailed design notes (state tables, retry policy, approval lifecycle).
