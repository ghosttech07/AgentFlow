"""Run a goal end to end against the running server and show live progress.

Usage (server must be running: uvicorn app.main:app):
    python scripts/run_workflow.py "Write a short beginner guide comparing EV adoption in India and China"
    python scripts/run_workflow.py "<goal>" --max-tasks 5 --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import httpx

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "AWAITING_APPROVAL"}


def _secs(task: dict) -> str:
    if not task.get("started_at"):
        return "-"
    start = datetime.fromisoformat(task["started_at"])
    end = datetime.fromisoformat(task["completed_at"]) if task.get("completed_at") else None
    if end is None:
        return "running"
    return f"{(end - start).total_seconds():.1f}s"


def print_table(wf: dict, elapsed: float) -> None:
    p = wf["progress"]
    print(f"\n[{elapsed:6.1f}s] workflow {wf['status']:<17} "
          f"done {p['succeeded']}/{p['total']}  running {p['running']}  failed {p['failed']}")
    if not wf["tasks"]:
        print("   (planning...)" if wf["status"] in ("PENDING", "PLANNING") else "   (no tasks)")
    for t in wf["tasks"]:
        deps = ",".join(t["depends_on"]) or "-"
        print(f"   {t['key']:<28} {t['agent_type']:<10} {t['status']:<17} {_secs(t):>8}   <- {deps}")
    sys.stdout.flush()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252 (no ₹, emoji)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    body: dict = {"name": args.goal[:80], "goal": args.goal, "run": True}
    if args.max_tasks:
        body["constraints"] = {"max_tasks": args.max_tasks}

    with httpx.Client(base_url=args.url, timeout=30) as client:
        resp = client.post("/workflows", json=body)
        if resp.status_code != 202:
            print(f"Create failed: {resp.status_code} {resp.text}")
            return 1
        wf_id = resp.json()["id"]
        print(f"Workflow {wf_id} accepted. Polling every {args.interval:g}s ...")

        t0 = time.monotonic()
        last = None
        while True:
            wf = client.get(f"/workflows/{wf_id}").json()
            snapshot = (wf["status"], [(t["key"], t["status"]) for t in wf["tasks"]])
            if snapshot != last:  # print only when something changed
                print_table(wf, time.monotonic() - t0)
                last = snapshot
            if wf["status"] in TERMINAL:
                break
            if time.monotonic() - t0 > args.timeout:
                print("Timed out waiting for the workflow.")
                return 1
            time.sleep(args.interval)

        total = time.monotonic() - t0
        run = f", {wf['duration_seconds']}s of execution" if wf["duration_seconds"] is not None else ""
        print(f"\nFinished: {wf['status']} after {total:.1f}s total (incl. planning){run}")
        if wf.get("context", {}).get("plan_summary"):
            print(f"Plan: {wf['context']['plan_summary']}")
        if wf["status"] != "COMPLETED":
            print(f"Error: {wf.get('error')}")
            return 1

        tasks = {t["key"]: t for t in wf["tasks"]}
        writer = next((t for t in tasks.values() if t["agent_type"] == "writer"), None)
        reviewer = next((t for t in tasks.values() if t["agent_type"] == "reviewer"), None)
        if writer and writer["output"]:
            print(f"\n=== {writer['output'].get('title', 'Final content')} "
                  f"({writer['output'].get('word_count')} words) ===\n")
            print(writer["output"].get("content", ""))
        if reviewer and reviewer["output"]:
            r = reviewer["output"]
            print(f"\n=== Review: score {r.get('score')}/10, approved={r.get('approved')} ===")
            print(r.get("summary", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
