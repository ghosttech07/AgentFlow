# Part 10 — Adaptive Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans (native, chosen by the user for Part 9 and
> asked again: "Build Native"). Steps use checkbox (`- [ ]`) syntax.

**Goal:** loops, branches, bounded re-planning and memory, all code-validated, budgeted, evented and crash-safe; plus
the Part 9 review leftovers, CI, Docker and secret scanning.

**Architecture:** conditions are an AST-whitelisted language (`app/core/conditions.py`); every graph change (loop
expansion, plan patch) goes through `app/services/graph_service.py` in one locked transaction; loop state lives in
`workflow.context.loops`; re-planning is a background planner job in a new `REPLANNING` state; memory is a Postgres
full-text table read by `engine/context.py` and written by the worker's success transaction.

**Spec:** `docs/superpowers/specs/2026-11-26-part10-adaptive-design.md` (incl. builder-check rulings 1-6, D1-D6).

## Global Constraints
- Plans without loops / run_if / join / replan_if / memory hits behave exactly as before; all existing tests pass.
- LLM proposes, code decides: conditions, loop decisions, patch validation, memory writes are code.
- `MAX_LOOP_ITERATIONS=5`, `MAX_REPLANS=2`, `REPLAN_MAX_NEW_TASKS=5`, `REPLAN_AUTO_APPROVE_MAX_TASKS=2`,
  `MEMORY_TOP_K=5`, `MEMORY_FACT_TTL_DAYS=30`, `EXFIL_MAX_URL_LENGTH=500`, `EXFIL_MAX_QUERY_CHARS=200`.
- Migration 0008. Loop keys `<base>__v<n>`. Test results always written to `reports/junit-<ts>.xml`.
- Default suite: fake LLM, no network. Gemini: at most 3 calls at the end.

## Review Focus
1. A loop whose reviewer output lacks the field `until` reads (e.g. no `score`) -> loop exhausted with
   condition_error, never a crash or an endless loop. Test in Task 9.
2. A replan patch that rewires dependents of a task that is RUNNING (not FAILED) -> rejected. Test in Task 10.
3. Two engines / a crash expanding the same loop iteration twice -> exactly one `__v2`. Test in Task 9.
4. A memory fact written from a tainted researcher, read by a communicator -> its send needs approval. Test in Task 11.
5. An exfil URL hidden in a redirect-free shortener on an allowlisted read domain -> allowed by design (read
   allowlist is trusted); documented. Test in Task 2 pins the behaviour.

---

### Task 1: CI, Docker, secrets hygiene, test reports
Files: `.github/workflows/ci.yml`, `pyproject.toml` (ruff config), `Dockerfile`, `.dockerignore`, `docker-compose.yml`
(service `app`, `profiles: [full]`), `.gitignore` (+ reports/, config/tools.yaml, config/models.yaml), `tests/conftest.py`
(`pytest_configure` sets `--junitxml=reports/junit-<UTC ts>.xml` unless given), `pytest.ini` (`-rfE`), README badge.
- [ ] Test: `tests/test_repo_hygiene.py` (gitignore lists the paths; `config/*.example.yaml` tracked; junit file appears
  under reports/ after a run is not unit-testable -> assert the conftest hook sets `xmlpath`).
- [ ] `ruff check .` passes locally (fix or narrowly ignore existing findings; record the rule set).
- [ ] `docker compose --profile full up -d --build` -> `/health` 200 (manual, logged).
- [ ] Push; poll `https://api.github.com/repos/ghosttech07/AgentFlow/actions/runs?branch=part10-adaptive` until
  the run is `success`. Commit `Part 10 T1: CI + Docker full profile + secrets scan + JUnit reports`.

### Task 2: Exfiltration guard for reads (0a)
Files: `app/tools/exfil.py` (`ExfilState`, `check_read_url(url, state, options) -> str | None` warning), `app/tools/loop.py`
(private_context from sql_query/file_read results; provenance set from web_search hits, fetched final_urls, task
input + dependency outputs; approval with the warning when check fails), settings caps. Tests
`tests/test_exfil.py`: sql_query -> evil page says fetch `https://evil.example/?d=<data>` -> approval with
"possible data exfiltration"; search-result links fetch without approval; caps; private-token flag; untainted +
non-private attempts unaffected.

### Task 3: Approval fixes 0b-0e
Files: `approval_service.py` (duplicate check: edited key already succeeded -> payload `already_sent_at`, approve
needs `confirm_duplicate`, 409 otherwise; strict bodies -> 422 listing allowed fields), `routes_approvals.py`
(`POST /approvals/{id}/check`), `schemas/control.py`, `loop.py` (rebuild injection warnings + untrusted sources from
journal rows on resume; DB-count cap on every execution path), dashboard card warning + confirm checkbox.
Tests `tests/test_p9_followups.py`: one per fix (RED first).

### Task 4: Flaky-test root cause (0f)
Rewrite `test_retrying_task_not_claimed_before_due` and `test_expired_approval_fails_workflow` without wall-clock
windows (assert against the stored `next_attempt_at` / `expires_at` captured at creation; drive expiry by updating
`expires_at` in the DB instead of sleeping); revert the widened windows. Run engine/tool suites 5x with JUnit; any
failure -> root cause + fix. Record findings in the ledger.

### Task 5: Schema + states
Migration 0008: tasks `loop_name`, `iteration` (default 1), `base_key`, `run_if`, `join` (default all_succeeded),
`replan_if`, `skip_reason`; `approvals.task_id` nullable; `plan_versions`; `memories` (+ GIN on generated tsvector).
State machine: workflow `REPLANNING` (RUNNING->REPLANNING->RUNNING|FAILED|CANCELLED; AWAITING_APPROVAL->REPLANNING),
task `SUPERSEDED` (FAILED->SUPERSEDED, PENDING/READY->SUPERSEDED terminal). Diagrams regenerated (sync test).
Budget `max_iterations_total`, `max_replans`. Settings from Global Constraints. Tests: schema roundtrip, transitions.

### Task 6: Condition language
`app/core/conditions.py`: `parse_condition(expr, known_keys) -> Condition` (raises `ConditionSyntaxError`),
`Condition.evaluate(outputs: dict[str, dict]) -> tuple[bool, dict]` (raises `ConditionError` on a missing field).
Tests `tests/test_conditions.py`: arithmetic/logic/in/len/lower/attr/index; escapes (`__class__`, `__import__`,
lambda, comprehension, `9**9**9`, length/depth limits); unknown ref at parse; missing field at runtime.

### Task 7: Plan model + validation + planner
`PlannedTask.run_if/join/replan_if`, `Plan.loops: list[LoopSpec]`, `Plan.replan_on_failure: bool = False`;
`dag.validate_plan` extended (loop chain, until refs, max_iterations, run_if refs to deps, key length for loops);
persistence of the new fields + `workflow.context.loops` init + `plan_versions` v1; planner prompt rules 13-15; fake
planner: one loop (write+review), China/India branch with run_if, `replan_on_failure: true`. Tests: validation cases,
planner prompt text, fake plan shape.

### Task 8: Branches + join-aware failure
`scheduler.py` promotion via a pure helper `app/core/joins.py::decide(task, deps) -> ready|skip|wait`;
run_if evaluation -> SKIPPED `condition_false` + event; condition_error -> FAILED; workflow fail rule join-aware
(ruling 2); `final_outputs` excludes skipped. Tests `tests/test_branches.py`: each join mode, run_if true/false with
values in the event, cascade, tolerated failure.

### Task 9: Loops
`app/services/graph_service.py::expand_loop(session, wf, loop, next_iteration) -> list[Task]` (idempotent),
`rewire_dependents`; scheduler evaluates `until` before promotion, blocks dependents of open loops (ruling 5),
on_exhausted modes incl. approval kind `loop_exhausted` (approve / reject / `extra_iterations`); budget pre-check;
writer `changes_made` + prompt; fake reviewer 5 -> 9; final outputs per ruling 4; events. Tests
`tests/test_loops.py` (all cases in the prompt + Review Focus 1 and 3).

### Task 10: Re-planning
`app/core/plan_patch.py` (`PlanPatch`, `validate_patch(...) -> list[PlanValidationError]`), planner patch mode,
`app/engine/runner_jobs.py::replan_job`, triggers (a)(b)(c), approval kind `plan_patch` (readable diff), apply in
`graph_service.apply_patch` (SUPERSEDED, rewire, plan_versions), endpoints `/replan`, `/plan-versions`, `/plan-patch`,
recovery. Tests `tests/test_replanning.py` (all cases in the prompt + Review Focus 2).

### Task 11: Memory
`app/services/memory_service.py` (`write_from_task`, `write_lesson`, `write_artifact_summary`, `search(session, query,
namespace, k)`, `pin`, `soft_delete`, `purge_expired`), worker hooks, `context.py` injection (PRIOR KNOWLEDGE, untrusted
delimiters, taint/private propagation, memory.used event + metrics), researcher `from_memory` + memory sources accepted
by verification, `app/api/routes_memory.py`, fake researcher answers from memory when covered. Tests
`tests/test_memory.py` (all prompt cases + Review Focus 4).

### Task 12: Visibility
Timeline (iterations grouped, condition/replan events with diffs, memory hits, summary counts incl. calls saved),
graph endpoint fields, dashboard (versions grouped in steps + Gantt, skip reasons, plan history with diffs,
plan_patch / loop_exhausted cards, Memory page). Tests: timeline summary, graph fields, dashboard presence.

### Task 13: adaptive_demo.py + regression demos
Scenarios 1-6 from the prompt with PASS/FAIL; chaos `crash_process` during `write_guide__v2`. All earlier demos PASS.

### Task 14: Live run, docs, final verification
Live: Ollama qwen3.5:4b + <= 3 Gemini calls showing a real review -> rewrite. Docs: CLAUDE.md, README (adaptive
features, condition syntax, loops, replanning, memory, exfil protection), ARCHITECTURE (dynamic expansion vs cycles,
replan sequence, memory flow, state diagrams), DEMO_GUIDE (4 new Q&A). Final: default suite green; engine/adaptive
suites 5x with JUnit; CI green; whole-branch review (opus) + fix pass; merge to main after the user's OK.

## User additions after plan review (binding, 2026-11-27)
1. Automatic re-plan after a failure is opt-in (planner-made plans); human POST /workflows/{id}/replan and the
   plan-patch while PAUSED work on ALL workflows, including manually created ones (test). -> Task 10
2. Loop body task keys > 46 chars: validator error "loop body task key must be <= 46 characters so __vN fits";
   test that the planner repair loop fixes it. -> Task 7
3. Task-less approvals (plan_patch): expiry, cancelled with the workflow, shown in the dashboard approvals view,
   decisions recorded in events with the actor (tests). -> Tasks 10, 12
4. Tolerated branch failures: workflow COMPLETES with result.failed_branches (keys + errors), event
   workflow.completed_with_failed_branches, dashboard warning badge, timeline summary count. -> Tasks 8, 12
5. until error + continue_with_best + best_by error -> fall back to the latest finished iteration, reason in the
   event. -> Task 9
If GitHub refuses the CI workflow push: stop and tell the user (they run gh auth refresh -s workflow).
