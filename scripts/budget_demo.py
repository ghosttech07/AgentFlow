"""Budget demo (Part 8, fake LLM, no quota): a workflow with max_llm_calls=3 pauses at the limit,
the budget is raised, it resumes and completes.

    python scripts/budget_demo.py [--port 8765]

Starts its own server (LLM_PROVIDER=fake); stop other servers on the same DB first.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from crash_demo import banner, start_server

GOAL = "Write a short market brief on solar power in Kenya"
SHOW = ("workflow.plan_created", "task.started", "task.succeeded", "workflow.budget_exceeded", "workflow.paused",
        "workflow.budget_updated", "workflow.resumed", "workflow.completed")


def wait_for(c: httpx.Client, wf_id: str, statuses: set[str], timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wf = c.get(f"/workflows/{wf_id}").json()
        if wf["status"] in statuses:
            return wf
        time.sleep(0.4)
    raise SystemExit(f"timed out waiting for {statuses}")


def show_events(c: httpx.Client, wf_id: str, since: int = 0) -> int:
    events = c.get(f"/workflows/{wf_id}/events").json()
    for e in events[since:]:
        if e["event_type"] in SHOW:
            p = e["payload"]
            detail = {k: p[k] for k in ("task_key", "limit", "reason", "actor", "from", "to", "usage", "provider")
                      if k in p and p[k] is not None}
            print(f"   {e['event_type']:<26} {detail}")
    return len(events)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    proc, log = start_server(args.port, True, "budget_demo")
    ok = True
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30) as c:
            banner("1. Run a workflow with budget {max_llm_calls: 3}")
            wf = c.post("/workflows", json={"name": "budget demo", "goal": GOAL, "run": True,
                                            "budget": {"max_llm_calls": 3}}).json()
            wf_id = wf["id"]
            wf = wait_for(c, wf_id, {"PAUSED", "COMPLETED", "FAILED"})
            print(f"status: {wf['status']}  usage: {wf['usage']}  remaining: {wf['remaining']}")
            seen = show_events(c, wf_id)
            ok &= wf["status"] == "PAUSED" and wf["usage"]["llm_calls"] == 3

            banner("2. PATCH /workflows/{id}/budget {max_llm_calls: 10}, then resume")
            r = c.patch(f"/workflows/{wf_id}/budget", json={"actor": "demo-operator", "budget": {"max_llm_calls": 10}})
            print(f"PATCH -> {r.status_code}; effective budget: {r.json()['effective_budget']}")
            r = c.post(f"/workflows/{wf_id}/resume", json={"actor": "demo-operator"})
            print(f"resume -> {r.status_code}, status {r.json()['status']}")
            wf = wait_for(c, wf_id, {"COMPLETED", "FAILED"})
            show_events(c, wf_id, seen)
            print(f"\nstatus: {wf['status']}  usage: {wf['usage']}")
            ok &= wf["status"] == "COMPLETED" and wf["usage"]["llm_calls"] == 6 and wf["usage"]["cost_usd"] == 0
            completed_tasks = [t["key"] for t in wf["tasks"] if t["status"] == "SUCCEEDED"]
            print(f"tasks succeeded: {completed_tasks}")
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()
    print(f"\nBudget demo - {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
