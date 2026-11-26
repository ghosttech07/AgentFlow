"""Local model reliability check (Part 9): does an Ollama model follow AgentFlow's tool action protocol?

For each model and role it runs the real tool loop (real journal, offline tools: fake web pages, the sample
database, calculator, sandboxed files) and the real planner, and reports:
  * success     - the run ended with a valid final answer (planner: a valid plan within the repair budget)
  * used tools  - runs that actually called at least one of the agent's tools
  * valid steps - share of model steps that were valid tool calls or a valid final answer
  * invalid     - tool calls rejected by code (unknown tool, bad arguments)
  * avg time    - seconds per run

    python scripts/local_model_check.py --runs 5
    python scripts/local_model_check.py --models llama3.2:3b --roles researcher,analyst --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.tools.catalog  # noqa: E402,F401  (registers every tool)
from app.agents import AgentInput, create_agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llm.circuit import CircuitBreaker  # noqa: E402
from app.llm.errors import LLMError  # noqa: E402
from app.llm.pricing import Pricing  # noqa: E402
from app.llm.providers.ollama_provider import make_ollama  # noqa: E402
from app.llm.router import ModelRouter  # noqa: E402
from app.llm.routes import RouteConfig  # noqa: E402
from app.models import Task, TaskAttempt, Workflow  # noqa: E402
from app.tools.config import ToolsConfig  # noqa: E402
from app.tools.loop import ToolLoop, ToolRuntime  # noqa: E402

TOOLS = ToolsConfig.model_validate({
    "tools": {"web_search": {"backends": ["fake"]}, "web_fetch": {"backend": "fake"},
              "sql_query": {"sources": {"sample": {"url_env": "SAMPLE_DATA_URL"}}}, "calculator": {},
              "file_write": {}},
    "agents": {"researcher": ["web_search", "web_fetch"], "analyst": ["sql_query", "calculator"],
               "writer": ["file_write"]}})
TASKS = {
    "researcher": ("Research EV sales in India", "Find the latest EV sales figures for India in 2026 and cite sources.",
                   {"search_queries": ["India EV sales 2026"]}, {}),
    "analyst": ("Analyse sample sales", "Query the sample ev_sales table for India's yearly units and compute the "
                "year-on-year growth with the calculator.", {"source": "sample"}, {}),
    "writer": ("Write and save a summary", "Write a 100-word summary of the results and save it as summary.md.",
               {}, {"research": {"summary": "India sold 2.3 lakh EVs in October 2026, up 31%; two-wheelers lead."}}),
}
GOAL = "Compare EV adoption in India and Indonesia in 2026 and write a short report"


def router(model: str) -> ModelRouter:
    s = get_settings()
    return ModelRouter(providers={"ollama": make_ollama(s)}, routes=RouteConfig(routes={"default": [f"ollama:{model}"]}),
                       pricing=Pricing({}), settings=s, session_factory=SessionLocal, breaker=CircuitBreaker(50, 1))


async def tool_run(model: str, role: str) -> dict[str, Any]:
    s = get_settings()
    name, desc, task_input, deps = TASKS[role]
    wf_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with SessionLocal() as db:
        db.add(Workflow(id=wf_id, name="local-model-check", goal=desc, status="RUNNING"))
        await db.flush()
        db.add(Task(id=task_id, workflow_id=wf_id, key=role, name=name, agent_type=role, status="RUNNING"))
        await db.flush()
        db.add(TaskAttempt(task_id=task_id, workflow_id=wf_id, attempt_number=1, worker_id="model-check"))
        await db.commit()
    agent = create_agent(role, router(model))
    tools = list(TOOLS.agents[role])
    rt = ToolRuntime(session_factory=SessionLocal, config=TOOLS, settings=s, workflow_id=wf_id, task_id=task_id,
                     task_key=role, attempt_number=1, tools=tools)
    agent.tool_runtime = rt
    loop = ToolLoop(agent, AgentInput(workflow_goal=desc, task_key=role, task_name=name, task_description=desc,
                                      task_input=task_input, dependency_outputs=deps), rt)
    start = time.monotonic()
    try:
        await loop.run()
        ok, error = True, None
    except (LLMError, TimeoutError) as exc:
        ok, error = False, f"{type(exc).__name__}: {str(exc)[:120]}"
    r = loop.report
    used = {c for c in r.by_tool if c in tools}
    async with SessionLocal() as db:  # throwaway workflow: never leave it in the user's history
        await db.delete(await db.get(Workflow, wf_id))
        await db.commit()
    return {"ok": ok, "steps": r.steps, "invalid": r.invalid_steps, "tool_calls": r.tool_calls,
            "used_tools": sorted(used), "seconds": time.monotonic() - start, "error": error}


async def planner_run(model: str) -> dict[str, Any]:
    from app.services.planning_service import plan

    start = time.monotonic()
    try:
        result = await plan(router(model), GOAL)
        return {"ok": True, "steps": 1 + result.repair_attempts, "invalid": result.repair_attempts, "tool_calls": 0,
                "used_tools": [], "seconds": time.monotonic() - start, "error": None}
    except Exception as exc:  # PlanningError or LLMError
        return {"ok": False, "steps": 3, "invalid": 3, "tool_calls": 0, "used_tools": [],
                "seconds": time.monotonic() - start, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="llama3.2:3b,qwen3:4b,qwen3.5:4b")
    ap.add_argument("--roles", default="researcher,analyst,writer,planner")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    rows = []
    for model in args.models.split(","):
        for role in args.roles.split(","):
            results = []
            for i in range(args.runs):
                res = await (planner_run(model) if role == "planner" else tool_run(model, role))
                results.append(res)
                print(f"{model:12} {role:10} run {i + 1}: {'ok  ' if res['ok'] else 'FAIL'} steps={res['steps']} "
                      f"invalid={res['invalid']} tools={res['used_tools']} {res['seconds']:.0f}s"
                      + (f" {res['error']}" if res['error'] else ""), flush=True)
            n = len(results)
            steps = sum(r["steps"] for r in results) or 1
            rows.append((model, role, sum(r["ok"] for r in results), n,
                         100 * (steps - sum(r["invalid"] for r in results)) / steps,
                         sum(r["invalid"] for r in results), sum(r["seconds"] for r in results) / n,
                         sum(bool(r["used_tools"]) for r in results) if role != "planner" else None))
    print("\n| Model | Role | Success | Used its tools | Valid steps | Invalid calls | Avg time |\n"
          "|---|---|---|---|---|---|---|")
    for model, role, ok, n, valid, invalid, secs, used in rows:
        used_txt = "n/a" if used is None else f"{used}/{n}"
        print(f"| {model} | {role} | {ok}/{n} | {used_txt} | {valid:.0f}% | {invalid} | {secs:.0f} s |")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
