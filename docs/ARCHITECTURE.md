# AgentFlow architecture

AgentFlow is a self-hosted engine for trustworthy AI workflows: AI does the work; code guarantees
reliability, cost limits and safety; humans approve under policy; every step is provable.

```
                 Dashboard (/dashboard)   REST API (/docs)   scripts/*.py
                          │                     │
                          ▼                     ▼
┌───────────────────────────────── FastAPI app ──────────────────────────────────┐
│ routes ─► services (workflow, planning, control, approval, budget, provider,   │
│           ask, timeline)  ─► state machine + events (one transaction each)     │
│                                                                                │
│ Engine loop (every tick re-reads the DB)                                       │
│   pickup run requests ─► reap expired leases ─► schedule (budget check,        │
│   approvals, promote, complete/fail) ─► claim READY tasks (SKIP LOCKED) ─►      │
│   worker pool (lease + heartbeat, call context = workflow/task)                │
│                                   │                                            │
│ Agents (planner, researcher, analyst, writer, reviewer, publisher)            │
│   content only; never touch status ─► LLMClient.generate_structured(role=…)    │
│                                   │                                            │
│   ┌───────────────────────── ModelRouter (Part 8) ─────────────────────────┐   │
│   │ route for the role (config/models.yaml: ordered "provider:model")      │   │
│   │  for each entry: circuit open? skip ─► budget pre-check / estimate     │   │
│   │  guard (pause + BudgetPause) ─► quota reserve (atomic upsert) ─►        │   │
│   │  adapter call ─► fallback on overloaded / quota / 404 / timeout,       │   │
│   │  stop on bad output / auth ─► price (pricing.yaml) ─► llm_calls row ─►  │   │
│   │  budget post-check (pause the workflow when a limit is reached)        │   │
│   └───────┬──────────────┬───────────────┬──────────────┬──────────┬────────┘   │
│           ▼              ▼               ▼              ▼          ▼            │
│        Gemini       Anthropic        OpenAI        Ollama (local)  Fake         │
│   (google-genai)  (anthropic SDK,  (openai SDK,   (openai SDK on   (tests,      │
│                    messages.parse)  chat.parse)    /v1, free)       demos)      │
└────────────────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
     PostgreSQL: workflows (+budget) · tasks · task_dependencies · workflow_events ·
     task_attempts (+provider, cost) · approvals · publications · chaos_rules ·
     llm_calls (one row per real model call) · provider_usage (quota windows)
```

## Request to result
1. `POST /workflows {goal, run: true, budget?}` stores the workflow (202).
2. The engine claims it, moves it to PLANNING and plans in the background. The planner call goes
   through the router like every other call (role `planner`) and counts toward the budget.
3. Each tick, `schedule_workflow` first checks the budget (a reached limit pauses the workflow),
   then expires approvals, fails or completes the workflow, promotes ready tasks and requests
   approvals for sensitive ones.
4. `claim_tasks` takes READY tasks of RUNNING workflows (approved ones only for gated tasks) with a
   lease. The worker builds the agent input from the DB, sets the call context and runs the agent.
5. The agent calls the router. The router walks the role's route, records an `llm_calls` row per real
   call and returns the answer with provider, model, cost, route position and fallback source.
6. The worker saves the result with the ownership guard (status + lease owner + attempt) in one
   transaction: task metrics and the attempt row carry provider and cost.

## Where each guarantee lives
| Guarantee | Code |
|---|---|
| Plans are valid before they reach the DB | `app/core/dag.py`, `planning_service.py` |
| No task runs twice; stale workers can't overwrite | `scheduler.claim_tasks`, `worker._save`, heartbeats |
| Crash recovery | `engine/recovery.py`, `scheduler.reap_expired_leases` |
| Retries with backoff | `engine/retry.py` |
| Cross-provider fallback + circuit breaker | `llm/router.py`, `llm/circuit.py` |
| Cost limits | `services/budget_service.py` (+ router pre/post checks, scheduler check) |
| Provider quotas survive restarts | `llm/quota.py` (`provider_usage`) |
| Human approval before sensitive steps | `services/approval_service.py`, claim-time check |
| Exactly-once side effects | `agents/publisher.py` (idempotency key) |
| Everything explainable afterwards | `workflow_events`, `llm_calls`, `services/timeline_service.py` |
