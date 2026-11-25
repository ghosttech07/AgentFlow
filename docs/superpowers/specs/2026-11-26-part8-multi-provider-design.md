# Part 8 design: multi-provider AI + cost control

Approved by the owner 2026-11-26 ("go ahead, build it, finish it"). Full requirements: the Part 8
prompt (summarised in CLAUDE.md, AgentFlow 2.0 section).

## Intent
AgentFlow must stop depending on one AI vendor and must keep cost and quota under code control:
every agent role routes to an ordered chain of `provider:model` entries across Gemini, Anthropic,
OpenAI and local Ollama; every call is priced; workflows pause (never fail) at their budget; an
exhausted provider is routed around. Positioning: code guarantees reliability, cost limits and safety.

## Goals / non-goals / anti-goals
- Goals: 4 providers behind the existing `LLMClient` protocol; YAML routes + quotas; pricing YAML;
  budgets on workflows; persisted per-provider usage; `/providers`, `/metrics/summary`, PATCH budget;
  dashboard + timeline show provider:model, fallbacks, cost, budget, providers.
- Non-goals: tools (Part 9), policy/roles (Part 11), streaming (Part 14).
- Anti-goals: any behaviour change with only GEMINI_API_KEY set and no new config; an LLM deciding
  routing, budgets or quotas; guessed prices; logged keys; real network in the default test suite.

## Constraints
Backward compatible; all existing tests keep passing; Gemini free tier (~20 calls/day) only for one
final live check; fake provider still overrides everything; Ollama is now installed locally
(llama3.2:3b, qwen3:4b); no Anthropic/OpenAI keys (adapters tested with mocked SDKs, skipped live).

## Approaches considered
1. **Router implementing LLMClient + one adapter per provider (chosen).** Agents unchanged except
   BaseAgent passing its role; fallback, breaker, quota and budget logic in one place.
2. Teach each agent about providers: duplicates fallback logic per agent; rejected.
3. Wrap LiteLLM or a similar gateway: hides error taxonomy and pricing control we must own, adds a
   heavy dependency; rejected.

## Decisions (builder's calls, stated)
- **Call context**: `generate_structured(..., role=None)` plus a contextvar `call_context`
  (workflow_id, task_id) set by the worker / planning job, so the router can count usage, check the
  budget and run the pre-call estimate guard without agents knowing about budgets.
- **Workflow totals** are SQL sums over `task_attempts` (cost_usd, tokens, llm_calls) plus a
  `workflow_llm_calls` row per planner call: one source of truth, no counter drift, written in the
  same transaction as the attempt. Planner calls are recorded as usage rows with task_id NULL.
- **Legacy default route** (no models.yaml): planner -> gemini:GEMINI_MODEL + fallbacks; workers ->
  gemini:GEMINI_WORKER_MODEL first when set (yesterday's behaviour), then the same chain.
- **Gemini adapter** = the existing GeminiClient moved behind the provider interface; its internal
  model-fallback loop is replaced by router entries (one model per call), behaviour preserved.
- **Breaker** generalised to `provider:model` keys (one process-wide instance).
- **Quota counting**: `provider_usage` row per (provider, model, window kind, window start),
  `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` before each real call; `exhausted` flag set on
  quota_exhausted. Window date in the entry's timezone (default QUOTA_TIMEZONE, per-provider override).
- **Budget pause** reuses control_service.pause (graceful) with actor "budget" and event
  workflow.budget_exceeded; claim refuses tasks of a workflow over budget.
- **Ollama** via its OpenAI-compatible endpoint with the openai SDK and JSON-schema response_format,
  if the installed Ollama honours it reliably (verified live); otherwise native /api/chat `format`.
- **Dashboard**: provider/budget/providers additions now; the full visual redesign runs in this
  part's dashboard step through the design skill, with a direction choice put to the owner.

## Left to the builder
Exact SDK call shapes (verified against installed anthropic 2.14 / openai 3.59), file layout inside
app/llm/providers/, test fixtures, prices in pricing.yaml (marked "verify against the provider's
current pricing page").
