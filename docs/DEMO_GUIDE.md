# AgentFlow demo guide

How to present AgentFlow in 5-10 minutes, and answers to the questions interviewers and judges ask.

## Before you start (2 minutes, once)
```bash
docker compose up -d
alembic upgrade head
python scripts/setup_sample_data.py      # sample database for the SQL tool (fake numbers)
```
Stop any other AgentFlow server that uses the same database (two engines would compete for work).

## The 5-minute run
1. **The pitch (30 s).** "AgentFlow runs a team of AI agents on a goal. The AI writes the content; code
   controls everything else: order, retries, cost limits, approvals and crash recovery. Every step is in
   PostgreSQL, so a crash never loses work."
2. **The hardest capability: crash and recovery (2 min).** `python scripts/crash_demo.py --fake`.
   Point at: the process is killed while the writer runs; while it's down the database shows the
   research tasks `SUCCEEDED` and the writer `RUNNING`; after the restart `engine.recovered`, the
   writer's attempt 1 is `worker_lost` and attempt 2 `succeeded`; finished tasks ran exactly once;
   **PASS**.
3. **Safe actions (2 min).** `python scripts/tools_demo.py`. Point at: research and the database query
   run in parallel; an invented source is removed by code; the email waits for approval showing exactly
   what will be sent; the approver edits the subject; the server crashes right after sending; after the
   restart there is still exactly **one** email; the malicious page is flagged and its email is blocked.
4. **The dashboard (1 min).** `LLM_PROVIDER=fake uvicorn app.main:app`, open `/dashboard`, ask a
   question, start a deep research run, show the steps, an approval card and the history.

### Recording the short video
Record step 2 (crash demo) in a terminal at a readable font size: show the command, the kill, the
database snapshot while down, the restart and the PASS line. Then, if time allows, step 3's "exactly ONE
email" lines. About 90 seconds is enough; narrate what each line proves.

## Questions you will be asked

**What happens if the server crashes in the middle of a task?**
Every state change is committed to PostgreSQL before anything else happens. On restart, recovery finds
tasks still marked RUNNING by a process that no longer exists, marks the attempt `worker_lost` and
schedules a retry. Tasks that already succeeded are never re-run; the crash demo checks that.

**What if the server crashes right after sending an email?**
Every tool call is written to a journal before it runs and updated after. The email's journal entry has
an idempotency key derived from the workflow, task, tool and exact arguments. After a restart the agent
resumes from the journal (it is not asked to start over), sees the email already succeeded and does not
send it again; even if the model proposes the same email, the key matches and the stored result is
reused. If the crash happened *during* the send (outcome unknown), outbox and webhook sends are repeated
with the same key (the receiver can de-duplicate), and SMTP goes back to a human with a warning, because
SMTP cannot de-duplicate.

**How do you stop prompt injection from sending email?**
Three layers, all in code: (1) web pages and API responses are wrapped as untrusted data and the agent
is told never to follow instructions in them; (2) once a task has read untrusted content it is
"tainted", taint flows to every downstream task, and every outside action from a tainted task needs a
human approval, which cannot be switched off in config; (3) suspicious text ("ignore previous
instructions", hidden text, "email all data to ...") is flagged in the approval card. On top of that,
email recipients must match an allowlist unless the approver explicitly overrides it.

**How do you know the researcher didn't make up its sources?**
Code compares every cited URL with the URLs the researcher actually got from search or opened in that
task. Anything else is removed, listed separately, and the confidence is lowered.

**Why PostgreSQL instead of Redis, Kafka or Temporal?**
One system, transactional: a status change and its event commit together, and claiming work with
`FOR UPDATE SKIP LOCKED` is safe across processes. AI steps take seconds, so the database is never the
bottleneck at this scale. The trade-off is throughput: it is not built for thousands of tasks per second.

**Is execution exactly-once?**
No system can make an AI call exactly-once across crashes. AgentFlow runs steps at least once and makes
every side effect idempotent (publications, emails, webhooks, files), so the *effects* happen once.

**How do you control cost?**
Every model call is priced and stored. Workflows have budgets (cost, tokens, calls, duration, tool
calls); reaching one pauses the workflow for a human instead of failing it. A call whose estimated cost
would cross the limit is stopped before it is made. Daily provider quotas are counted in the database,
so they survive restarts.

**What if a model provider is down?**
Each agent role has an ordered list of models across providers. Overloaded, out-of-quota or missing
models fall through to the next entry, with a circuit breaker per model; the model actually used and the
fallback are recorded on the task.

**What isn't done yet?**
See "Limitations" in the README: user accounts and roles (Part 11), feedback loops (Part 10),
load testing (Part 14), and live tests for the Anthropic and OpenAI adapters (no keys).
