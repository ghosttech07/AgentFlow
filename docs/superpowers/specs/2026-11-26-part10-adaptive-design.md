# Part 10 — Adaptive workflows (design)

Status: draft for the user's review (decisions that differ from the prompt are listed at the end). Branch
`part10-adaptive` (from main after Part 9).

## Intent
Workflows adapt instead of following a fixed plan: review-driven rewrite loops, conditional branches, bounded
re-planning mid-run and memory between workflows. Every adaptation is decided or validated by code, bounded,
budgeted, recorded as events and crash-safe. Step 0 first closes the Part 9 review leftovers (GET
exfiltration, duplicate-send, strict approval bodies, warnings after a crash, tool-call cap, flaky tests) and adds
CI, Docker and secret scanning.

## Non-goals
Policy engine / users (11), hash-chained audit (12), evals (13). No real cycles in the stored graph. No
pgvector in this part (see D3).

## Step 0 — fixes + hygiene
- **0a exfiltration through reads.** An attempt is `private_context` once it read `sql_query`, `file_read` or a
  private memory. In a private OR tainted attempt, `web_fetch` / `http_request GET` runs without approval only if
  the host is in a read allowlist (`tools.<tool>.read_allowlist`) or the exact normalized URL (query included)
  appeared earlier in this task (a web_search result URL, a fetched final_url) or in the task input / dependency
  outputs ("link provenance"). Otherwise -> tool-call approval with warning "possible data exfiltration: URL not
  from a trusted source" and the full URL. Such attempts also get caps (`EXFIL_MAX_URL_LENGTH` 500,
  `EXFIL_MAX_QUERY_CHARS` 200; over the cap -> approval) and a flag when a query value contains a >=6-char
  token from an earlier private tool result.
- **0b duplicate send.** When approved/edited arguments hash to a key that already `succeeded`, the approval
  payload/card shows "this exact message was already sent at <time>", and approve requires
  `confirm_duplicate: true` (409 otherwise). The approval endpoint computes this on approve; the dashboard
  also shows it on the card by asking `GET /approvals/{id}` (payload field `already_sent_at`, refreshed when the
  card's arguments are edited via `POST /approvals/{id}/check`).
- **0c strict approval bodies.** Approve/reject reject fields that do not apply to the approval kind (422 listing
  the allowed fields): `input_overrides` on tool_call, `arguments_overrides` on task, etc.
- **0d warnings after a crash.** On resume, `injection_warnings` and untrusted sources are rebuilt from the
  journal rows (not only from `loop_state`).
- **0e tool-call cap.** `max_tool_calls` is checked with the DB count before every execution path (new call,
  approved call, unknown-outcome re-run).
- **0f flaky tests.** pytest always writes JUnit XML to `reports/junit-<timestamp>.xml` (gitignored) and prints
  failed test ids (`-rfE`). Engine/tool suites run 5x; failures are root-caused. The two widened tests are
  rewritten to not depend on wall-clock windows (inject the clock / poll for a condition).
- **0g CI + Docker.** `.github/workflows/ci.yml`: Python 3.11, `postgres:16` service, create `agentflow_test`,
  `scripts/setup_sample_data.py`, `alembic upgrade head`, `pytest` (fake LLM), `ruff check` (config in
  `pyproject.toml`, rules start conservative: E/F/I/B/UP subset, existing code made to pass). `Dockerfile` +
  compose service `app` under `profiles: [full]` (migrations + sample data + uvicorn, `LLM_PROVIDER=fake` unless
  overridden). README badge.
- **0h secrets.** gitleaks GitHub Action in CI; `.gitignore` adds `reports/`, `config/tools.yaml`,
  `config/models.yaml`; keeps `*.example.yaml`; history scan result reported (clean on 2026-11-26).

## 1. Condition language (`app/core/conditions.py`)
AST whitelist (shares the calculator's evaluator style): literals, `and/or/not`, comparisons incl. `in`,
arithmetic, `len()`, `lower()`, `min/max`, attribute access `task_key.output.field[.field]`, list indexing,
`None/True/False`. Names must be task keys (or loop base keys, resolved to the latest finished iteration).
Limits: 300 chars, depth 12, node count 80, exponent bound. `parse_condition(expr, known_keys)` at plan time
(unknown reference -> plan error); `evaluate(expr, outputs) -> (bool, values_used)`; a missing field at run time
-> `ConditionError` -> the task FAILED with `error_type="condition_error"` (not a crash; not retried).

## 2. Feedback loops (dynamic expansion, no stored cycles)
Plan field `loops: [{name, body: [keys...], until, max_iterations, on_exhausted, best_by?, feedback: {from, to}}]`.
- Validation: body is a contiguous dependency chain (each body task depends on the previous); `until` parses
  with references inside the body; `max_iterations <= MAX_LOOP_ITERATIONS` (5); feedback tasks in the body.
- Tasks get columns `loop_name`, `iteration` (1..n), `base_key`. Iteration 1 = the planned tasks.
- When the last body task of iteration k SUCCEEDS, the scheduler (inside its locked transaction) evaluates
  `until` over that iteration's outputs: event `loop.condition_evaluated {expression, result, values}`.
  - true -> done (`loop.completed`).
  - false and k < max and budget allows -> `graph_service.expand_loop` inserts `<base>__v{k+1}` for each body
    task: the first body task depends on its own previous iteration + the previous iteration's feedback source
    + its original non-body dependencies; later body tasks depend on the new previous body task (+ original
    non-body deps). The `to` task's input gets `revision_feedback` = the `from` task's issues + suggestions +
    summary + the previous draft. Dependents of the loop's last body task are rewired to the new last task.
    Event `loop.iteration_started {iteration}`. Uniqueness: `UNIQUE(workflow_id, key)` already exists, plus the
    expansion checks `(loop_name, iteration)` before inserting -> idempotent after a crash.
  - false and exhausted -> `on_exhausted`: `continue_with_best` (picks the iteration with the max `best_by`,
    default `<reviewer>.output.score`; dependents rewired to that iteration's last task; event
    `loop.exhausted`), `fail` (workflow FAILED), `ask_human` (approval kind `loop_exhausted`: approve =
    continue with best; reject = fail; approve with `extra_iterations: n` (<= remaining global cap) = expand again).
- Budget pre-check: if `max_iterations_total` is reached, or the estimated cost of one more iteration (the sum of
  the iteration's LLM calls so far) would cross `max_llm_calls` / `max_cost_usd`, treat as exhausted with reason.
- Writer output gets optional `changes_made: list[str]`; its prompt says: when `revision_feedback` exists,
  address every high/medium issue and list what changed. Planner prompt: loops for write+review by default
  (max_iterations 2-3). Fake planner: one loop; fake reviewer scores v1 5 / v2 9.

## 3. Conditional branches
`PlannedTask.run_if` (expression over its dependencies) and `join: all_succeeded | any_succeeded | all_done`.
Promotion rules (scheduler): a PENDING task is evaluated when every dependency is finished
(SUCCEEDED/SKIPPED/FAILED/SUPERSEDED-replaced):
- all_succeeded: any SKIPPED dep -> SKIPPED (cascade, current behaviour); any FAILED dep -> wait (workflow fails
  as today).
- any_succeeded: at least one SUCCEEDED -> eligible; all SKIPPED -> SKIPPED.
- all_done: eligible once every dep is finished (SKIPPED or SUCCEEDED).
Then `run_if` false -> SKIPPED `reason=condition_false` + event `task.condition_evaluated {expression, values}`.
Graph endpoint: nodes carry `run_if`, `join`, `skip_reason`, `loop_name`, `iteration`.

## 4. Re-planning (bounded, validated)
- New statuses: workflow `REPLANNING`; task `SUPERSEDED` (terminal). State machine + diagrams + sync test.
- Triggers: (a) a task FAILED for good while `replan_on_failure` is on for the workflow (see D2);
  (b) `PlannedTask.replan_if` true when that task succeeds; (c) `POST /workflows/{id}/replan {actor, instructions}`.
- Flow: RUNNING -> REPLANNING (`workflow.replan_started`, reason, trigger), committed before the planner call
  (like PLANNING). Running tasks keep going; the scheduler claims nothing for a REPLANNING workflow. A background
  job (like `runner_jobs.plan_and_start`) calls the planner in "patch mode" with the goal, the graph + statuses,
  short summaries of succeeded outputs, failure reasons, remaining budget and instructions -> `PlanPatch {summary,
  add_tasks[PlannedTask], cancel_tasks[keys], rewire: [{from_failed, to_replacement}]}`.
- Code validation (`app/core/plan_patch.py`): never touch SUCCEEDED/RUNNING/AWAITING_APPROVAL/approved tasks;
  cancel only PENDING/READY never started; new keys unique; the merged graph passes `validate_plan`; sensitive
  policy on new tasks; `<= REPLAN_MAX_NEW_TASKS` (5); `<= MAX_REPLANS` (2) per workflow; budget estimate fits.
  Invalid -> repair loop (`PLANNER_MAX_REPAIR_ATTEMPTS`) -> still invalid -> replan failed: trigger (a)/(b) ->
  workflow FAILED with the reason; trigger (c) -> back to the previous status + event.
- Approval kind `plan_patch` when: a sensitive task is added, more than `REPLAN_AUTO_APPROVE_MAX_TASKS` (2) tasks
  are added, or the failed task was sensitive. Otherwise auto-applied.
- Apply (one transaction, idempotent by patch id): insert tasks + edges, cancel listed tasks, rewire dependents,
  the failed task -> SUPERSEDED when its dependents move to a replacement, `plan_versions` row, back to RUNNING.
- `plan_versions(id, workflow_id, version, patch, reason, trigger, created_by, approved_by, status, created_at)`;
  version 1 = the original plan (written at planning time). `GET /workflows/{id}/plan-versions`.
  `POST /workflows/{id}/plan-patch` (human patch, only while PAUSED; same validation; actor recorded).
- Recovery: a REPLANNING workflow at startup -> back to RUNNING (or its stored previous status) and the replan is
  re-triggered (patches are applied only in one transaction, so nothing partial exists).

## 5. Memory
Table `memories` (migration 0008, see D1) with the prompt's columns; `search_vector` generated
`to_tsvector('english', content)` + GIN index.
- Writes (code only, in the worker's success transaction): researcher -> one `fact` per key finding that cites a
  VERIFIED source (sources stored = verified URLs; findings without one are not stored); TTL
  `MEMORY_FACT_TTL_DAYS` 30. Loop end -> `lesson` from recurring reviewer issues (TTL 180 d). Workflow
  COMPLETED -> `artifact_summary`. Flags `tainted` / `private` copied from the source task. Settings
  `MEMORY_ENABLED` (true) and workflow `memory: {read, write}` (default both true).
- Reads (`context.build_agent_input`): top `MEMORY_TOP_K` (5) non-expired, non-deleted memories by
  `ts_rank(search_vector, websearch_to_tsquery(goal + task description))` weighted by recency and confidence,
  pinned first; only when a minimum rank is met. Injected as "PRIOR KNOWLEDGE (from earlier workflows; may be
  outdated; verify if important)" with dates and sources; tainted memories inside untrusted delimiters and they
  taint the task; private memories make it private_context. Memory hits recorded in task metrics + event
  `memory.used`.
- Researcher: may answer from memory (output `from_memory: list[str]`), source verification accepts URLs of
  verified memory facts used in this task.
- API: `GET /memories?q=&kind=&namespace=`, `GET /memories/{id}`, `POST /memories/{id}/pin`,
  `DELETE /memories/{id}` (soft delete + event), `POST /memories/purge-expired`.

## 6. Engine, budgets, visibility
All graph changes go through `app/services/graph_service.py` (row locks, one transaction, events). Budget fields
`max_iterations_total`, `max_replans`. Timeline: iterations grouped (v1/v2/v3) in spans, condition and replan
events with diffs, memory hits; summary adds iterations, replans, memory hits, estimated LLM calls saved.
Dashboard: grouped versions in the steps list and Gantt, skipped-by-condition reason, plan version history with
diffs, approval cards for `plan_patch` and `loop_exhausted`, a Memory page in the sidebar.

## Sequence: loop expansion
```mermaid
sequenceDiagram
  participant W as Worker
  participant S as Scheduler (locked tx)
  participant G as graph_service
  W->>S: review_guide SUCCEEDED (score 5)
  S->>S: evaluate until -> false (event with values)
  S->>G: expand_loop(improve_draft, 2) (no-op if v2 exists)
  G->>G: insert write_guide__v2 (+revision_feedback), review_guide__v2; rewire dependents
  S-->>W: write_guide__v2 READY ... review v2 = 9 -> until true -> loop.completed
```

## Risks
- Conditions/loops are new failure surfaces in the scheduler's hot path: every evaluation is guarded and errors
  become `condition_error`, never exceptions out of the tick.
- Re-planning with a small local planner may produce invalid patches; bounded by repairs + MAX_REPLANS.
- Memory poisoning: tainted memories are delimited and taint the reader; only verified-source facts are stored.
- CI on GitHub needs a token with `workflow` scope to push `.github/workflows`; if the push is refused, the user
  must push that file or grant the scope.

## Decisions that differ from the prompt (for the user)
- **D1 Migration number:** the prompt says 0007, but 0007 already exists (Quick Ask history). Part 10 uses 0008.
- **D2 Replan-on-failure is opt-in per workflow** (`adaptive.replan_on_failure`, the planner sets it to true by
  default; manual plans and existing tests keep "a failed task fails the workflow"). Reason: the prompt's
  backward-compatibility rule; a global default would change v1 failure behaviour.
- **D3 Memory search is Postgres full-text only** (pgvector is optional in the prompt; not built in this part;
  noted as future work). Reason: no extension install on the user's Docker image needed; FTS is required anyway.
- **D4 Flaky-test root cause:** the two tests widened in Part 9 are rewritten to remove wall-clock windows (and
  the earlier "1 s -> 5 s" / "0.3 s -> 2 s" changes are reverted to strict assertions on a controlled clock).
- **D5 0b duplicate check** happens on approve (409 unless `confirm_duplicate`), and the card shows the warning
  from the payload; live re-check while editing is a small `POST /approvals/{id}/check` endpoint.
- **D6 Loop keys** use `<base>__v<n>` (fits the existing key rule; base keys longer than 46 chars cannot loop —
  validation error).
