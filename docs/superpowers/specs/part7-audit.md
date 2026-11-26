# Part 7 audit (observability & demo), 2026-11-26

Each item of the Part 7 spec checked against the repository. "Fixed now" items were added during Part 9.

| Item | Status | Evidence / what was done |
|---|---|---|
| Timeline API (`GET /workflows/{id}/timeline`: summary, spans, readable entries, built from the DB) | Done | `app/services/timeline_service.py`; `tests/test_timeline.py`; Part 9 added tool summary + per-attempt tool markers |
| Metrics summary | Done | `GET /metrics/summary` (Part 8: calls, errors, latency, cost per provider/entry; Part 9: per tool) |
| Structured logging | Done (key=value lines) | `app/core/logging.py`; one line per record, `key=value` messages; no JSON output mode |
| Request id | **Fixed now** | `X-Request-ID` middleware (echoes a safe incoming id or generates one), `rid=` on every log line written while handling the request; `tests/test_request_id.py` |
| Dashboard: workflow list, create, progress, controls, approvals, result | Done | Redesigned as an answer engine (history sidebar, Deep research, approval cards, steps, report) |
| Dashboard: Gantt (attempt bars) | **Fixed now** | The redesign had dropped it; restored as the "Timeline" chart in a research view's Details (bars per attempt by outcome, tool-call ticks) |
| Dashboard: chaos button hidden in production | **Fixed now** | "Simulate a crash" in Details, shown only when `GET /dev/chaos` answers (it is 404 in production) |
| Dashboard: safe rendering, no raw innerHTML | Partial (safe) | All server strings go through `esc()`; Markdown is escaped first and only rebuilt for headings/lists/bold. The page still assigns escaped HTML with `innerHTML` (no DOM-builder rewrite) |
| `scripts/demo.py` with PASS checks | **Fixed now** | Runs crash, controls, budget and tools demos and prints one PASS/FAIL table. The individual demos (`crash_demo.py` etc.) existed already |
| Docker "full" profile (app + database in one `docker compose --profile full up`) | **Missing (bigger)** | Only the Postgres service exists; there is no app Dockerfile. Needs a Dockerfile, a compose service under `profiles: [full]` that runs migrations + uvicorn, and a build test. For you to decide. |
| CI workflow + ruff | **Missing (bigger)** | No `.github/workflows`, no ruff config. Needs a GitHub Actions job with a Postgres service running `pytest`, a ruff config, and a pass over existing lint findings. For you to decide. |
| README / ARCHITECTURE / DEMO_GUIDE | Done | README rewritten (trade-offs, limitations); `docs/ARCHITECTURE.md` (diagram + state diagrams with a sync test); `docs/DEMO_GUIDE.md` added in Part 9 |
| Part 7 tests | Done | `tests/test_timeline.py` (timeline story, dashboard served), crash demo PASS; all run in the default suite |
