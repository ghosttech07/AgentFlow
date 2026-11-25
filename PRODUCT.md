# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
One owner-operator who uses AgentFlow as a daily research tool (ask quick questions, run research
reports, approve publishing, check what happened) and who also demos it to recruiters, judges and
clients. Both uses are equally important: the dashboard must be a fast working tool and must explain
itself during a live demo.

## Product Purpose
AgentFlow turns a goal into finished, cited research work using a team of AI agents (planner,
researcher, analyst, writer, reviewer, publisher), reliably and under human control. Success: fast,
accurate, current answers and reports with real sources; no lost work on crashes; nothing sensitive
happens without approval; every action is explainable afterwards.

## Positioning
Durable, auditable multi-agent execution in one small codebase: every step is persisted in
PostgreSQL, crashes resume exactly where they stopped (proven by a crash demo), sensitive steps wait
for a human decision, side effects happen exactly once, and the timeline explains every action.
Research uses live web pages with code-enforced citations.

## Operating Context
- Local FastAPI server (`uvicorn app.main:app`), dashboard at `/dashboard`, API docs at `/docs`.
- Quick Ask (`POST /ask`): question -> live web search -> cited answer in ~3-15 s.
- Workflows: goal -> plan -> parallel agents -> optional approval -> result; pause/resume/cancel/retry.
- Demos: `scripts/crash_demo.py` (crash + recovery), `scripts/controls_demo.py` (controls/approvals).
- Free-tier constraints: Gemini free quota (~20 planner calls/day; workers on a lite model), free
  metasearch for web results (can be intermittently thin).

## Capabilities and Constraints
- Agents produce text (research, analysis, reports, reviews); publisher writes Markdown files.
- No email, code execution, design or website building yet.
- Dashboard is one static HTML file with no build step and no external JS libraries; it only uses
  the public API. Optional X-API-Key for control actions.

## Brand Commitments
Name "AgentFlow". No other visual commitments (owner: "your call").

## Evidence on Hand
Real outputs from live runs (e.g. Oct 2026 India EV sales cited from Vahan-based sites), test suite
(300+ tests), crash demo PASS output. No customers, testimonials or benchmarks exist; do not invent any.

## Product Principles
1. Answers first: the fastest path from a question to a cited answer comes first.
2. Show the evidence: sources, timings and the event trail are always one glance away.
3. The human stays in charge: decisions and controls are obvious and never ambiguous.
4. Honest about limits: thin sources, low confidence and failures are shown plainly.
