"""Human-controls demo (Part 6): pause/resume, approve, reject, cancel. Fake LLM only (no quota).

    python scripts/controls_demo.py                # port 8765, admin key "demo-admin-key"
    python scripts/controls_demo.py --port 8766 --api-key my-key

Starts its own uvicorn with LLM_PROVIDER=fake and ADMIN_API_KEY set (stop other servers on the same
DB first). Scenarios: A approve + pause/resume, B reject (skip_task), C cancel. Prints PASS/FAIL each.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from crash_demo import banner, get_settings, start_server

PUBLISH_GOAL = "Write a short beginner guide on EV adoption in India and publish it"


def line(wf: dict) -> str:
    return f"{wf['status']:<17} " + "  ".join(f"{t['key']}={t['status']}" for t in wf["tasks"])


def wait_for(c: httpx.Client, wf_id: str, pred: Callable[[dict], bool], what: str, timeout: float = 90) -> dict:
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        wf = c.get(f"/workflows/{wf_id}").json()
        if line(wf) != last:
            print(f"   {line(wf)}", flush=True)
            last = line(wf)
        if pred(wf):
            return wf
        time.sleep(0.3)
    raise SystemExit(f"Timed out waiting for: {what}")


def any_running(wf: dict) -> bool:
    return any(t["status"] == "RUNNING" for t in wf["tasks"])


def started_count(c: httpx.Client, wf_id: str) -> int:
    return sum(e["event_type"] == "task.started" for e in c.get(f"/workflows/{wf_id}/events").json())


async def publication_count(key: str) -> int:
    import asyncpg

    conn = await asyncpg.connect(get_settings().database_url.replace("+asyncpg", ""))
    try:
        return await conn.fetchval("SELECT count(*) FROM publications WHERE idempotency_key = $1", key)
    finally:
        await conn.close()


def create(c: httpx.Client, goal: str) -> str:
    r = c.post("/workflows", json={"name": goal[:60], "goal": goal, "run": True})
    r.raise_for_status()
    print(f"Workflow {r.json()['id']} accepted: {goal!r}")
    return r.json()["id"]


def check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))
    print(f"   [{'ok' if ok else 'FAIL'}] {label}", flush=True)


def scenario_a(c: httpx.Client) -> bool:
    banner("Scenario A: pause/resume during research, then approve the publish step")
    r: list[tuple[str, bool]] = []
    wf_id = create(c, PUBLISH_GOAL)
    wait_for(c, wf_id, any_running, "research running")
    unauth = c.post(f"/workflows/{wf_id}/pause", headers={"X-API-Key": "wrong"})
    check(r, f"pause with a wrong X-API-Key -> {unauth.status_code}", unauth.status_code == 401)

    wf = c.post(f"/workflows/{wf_id}/pause", json={"actor": "demo-operator", "reason": "checking sources",
                                                    "mode": "graceful"}).json()
    print(f"PAUSED (graceful) by demo-operator: {line(wf)}")
    starts = started_count(c, wf_id)
    for i in range(5):
        time.sleep(1)
        print(f"   +{i + 1}s {line(c.get(f'/workflows/{wf_id}').json())}", flush=True)
    wf = c.get(f"/workflows/{wf_id}").json()
    check(r, "no task started while paused (5 s)", started_count(c, wf_id) == starts and not any_running(wf))
    check(r, "in-flight research finished and was saved",
          all(t["status"] == "SUCCEEDED" for t in wf["tasks"] if t["key"].startswith("research")))

    wf = c.post(f"/workflows/{wf_id}/resume", json={"actor": "demo-operator"}).json()
    print(f"RESUMED: {wf['status']}")
    wf = wait_for(c, wf_id, lambda w: w["status"] == "AWAITING_APPROVAL", "approval request")
    approval = c.get("/approvals", params={"status": "pending", "workflow_id": wf_id}).json()[0]
    print("\nApproval request (what a human sees before anything is published):")
    print(json.dumps(approval["request_payload"], indent=2)[:2500])

    title = "EV Adoption in India: A Beginner's Guide (approved edition)"
    resp = c.post(f"/approvals/{approval['id']}/approve",
                  json={"actor": "demo-reviewer", "comment": "Looks good; fixing the title.",
                        "input_overrides": {"title": title}})
    check(r, f"approve by demo-reviewer -> {resp.status_code}", resp.status_code == 200)
    wf = wait_for(c, wf_id, lambda w: w["status"] in ("COMPLETED", "FAILED"), "completion")
    check(r, "workflow COMPLETED", wf["status"] == "COMPLETED")

    pub = next(t for t in wf["tasks"] if t["agent_type"] == "publisher")["output"] or {}
    path = Path(pub.get("location", ""))
    print(f"\nPublished file: {path}")
    if path.is_file():
        print("".join(f"   | {ln}\n" for ln in path.read_text(encoding="utf-8").splitlines()[:8]))
    check(r, "file exists and carries the approved title override",
          path.is_file() and title in path.read_text(encoding="utf-8"))
    again = c.post(f"/workflows/{wf_id}/retry")
    check(r, f"POST /retry on the COMPLETED workflow -> {again.status_code}", again.status_code == 409)
    count = asyncio.run(publication_count(pub.get("idempotency_key", "")))
    check(r, f"publications rows for {pub.get('idempotency_key')}: {count}", count == 1)
    return all(ok for _, ok in r)


def scenario_b(c: httpx.Client) -> bool:
    banner("Scenario B: reject the publish step (skip_task)")
    r: list[tuple[str, bool]] = []
    wf_id = create(c, PUBLISH_GOAL)
    wait_for(c, wf_id, lambda w: w["status"] == "AWAITING_APPROVAL", "approval request")
    approval = c.get("/approvals", params={"status": "pending", "workflow_id": wf_id}).json()[0]
    resp = c.post(f"/approvals/{approval['id']}/reject",
                  json={"actor": "demo-reviewer", "reason": "Not ready to publish", "on_reject": "skip_task"})
    check(r, f"reject (skip_task) -> {resp.status_code}", resp.status_code == 200)
    wf = wait_for(c, wf_id, lambda w: w["status"] in ("COMPLETED", "FAILED"), "completion")
    skipped = (wf.get("result") or {}).get("skipped", [])
    print(f"result.skipped = {skipped}")
    check(r, "workflow COMPLETED with the publisher skipped",
          wf["status"] == "COMPLETED" and "publish_report" in skipped)
    count = asyncio.run(publication_count(f"{wf_id}:publish_report"))
    published = list(get_settings().publish_dir.glob(f"{wf_id}__*.md"))
    check(r, f"nothing published (rows={count}, files={len(published)})", count == 0 and not published)
    return all(ok for _, ok in r)


def scenario_c(c: httpx.Client) -> bool:
    banner("Scenario C: cancel while tasks are RUNNING")
    r: list[tuple[str, bool]] = []
    wf_id = create(c, "Write a short note on solar power in Kenya")
    wf = wait_for(c, wf_id, any_running, "a running task")
    running = [t["key"] for t in wf["tasks"] if t["status"] == "RUNNING"]
    wf = c.post(f"/workflows/{wf_id}/cancel", json={"actor": "demo-operator", "reason": "changed priorities"}).json()
    print(f"CANCELLED while running {running}: {line(wf)}\nerror: {wf['error']}")
    print("Waiting 5 s (longer than one fake agent call) to prove the in-flight result is discarded ...")
    time.sleep(5)
    wf = c.get(f"/workflows/{wf_id}").json()
    print(f"   {line(wf)}")
    tasks = {t["key"]: t for t in wf["tasks"]}
    check(r, "workflow CANCELLED, every task CANCELLED",
          wf["status"] == "CANCELLED" and all(t["status"] == "CANCELLED" for t in wf["tasks"]))
    check(r, "in-flight results discarded (no output saved)", all(tasks[k]["output"] is None for k in running))
    outcomes = [a["outcome"] for k in running for a in c.get(f"/workflows/{wf_id}/tasks/{k}/attempts").json()]
    check(r, f"attempt outcomes of the running tasks: {outcomes}", outcomes and set(outcomes) == {"cancelled"})
    event = next(e for e in c.get(f"/workflows/{wf_id}/events").json() if e["event_type"] == "workflow.cancelled")
    print(f"workflow.cancelled payload: {event['payload']}")
    check(r, "event records actor and reason",
          event["payload"].get("actor") == "demo-operator" and event["payload"].get("reason") == "changed priorities")
    return all(ok for _, ok in r)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252 (no ₹, emoji)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--api-key", default="demo-admin-key")
    args = ap.parse_args()
    os.environ["ADMIN_API_KEY"] = args.api_key  # inherited by the server subprocess
    proc, log = start_server(args.port, True, "controls_demo")
    results: dict[str, bool] = {}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30,
                          headers={"X-API-Key": args.api_key}) as c:
            results["A (pause/resume + approve)"] = scenario_a(c)
            results["B (reject, skip_task)"] = scenario_b(c)
            results["C (cancel while running)"] = scenario_c(c)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()
    banner("Results")
    for name, ok in results.items():
        print(f"   Scenario {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
