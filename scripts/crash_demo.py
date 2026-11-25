"""Crash-recovery demo: run a workflow, kill the server mid-task, restart it, watch it finish.

    python scripts/crash_demo.py --fake        # canned agent outputs (no Gemini calls)
    python scripts/crash_demo.py               # real Gemini
    python scripts/crash_demo.py --fake --port 8765 --goal "..."

Starts its own uvicorn (do not run another server on the same DB: in single-node mode each
engine treats every RUNNING task as its own orphan). Server logs go to logs/crash_demo_run*.log.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

DEFAULT_GOAL = "Write a short beginner guide comparing EV adoption in India and China"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "AWAITING_APPROVAL"}


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}", flush=True)


def start_server(port: int, fake: bool, log_name: str) -> tuple[subprocess.Popen, object]:
    env = {**os.environ, "ENGINE_ENABLED": "true", "PYTHONUNBUFFERED": "1"}
    if fake:
        env["LLM_PROVIDER"] = "fake"
        env["WEB_SEARCH_PROVIDER"] = "none"  # deterministic, offline demo run
    (ROOT / "logs").mkdir(exist_ok=True)
    log = open(ROOT / "logs" / f"{log_name}.log", "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"Server exited during startup (code {proc.returncode}); see {log.name}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                print(f"Server up on port {port} (pid {proc.pid}, log {log.name})", flush=True)
                return proc, log
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise SystemExit("Server did not become healthy in 30s")


def show(wf: dict, t0: float) -> None:
    p = wf["progress"]
    print(f"[{time.monotonic() - t0:6.1f}s] {wf['status']:<12} done {p['succeeded']}/{p['total']}  "
          + "  ".join(f"{t['key']}={t['status']}" for t in wf["tasks"]), flush=True)


async def db_snapshot(wf_id: str) -> list[tuple]:
    """Read task state straight from Postgres (the server is dead, so no API)."""
    import asyncpg

    conn = await asyncpg.connect(get_settings().database_url.replace("+asyncpg", ""))
    try:
        return await conn.fetch(
            "SELECT key, status, attempt_count, lease_owner, lease_expires_at FROM tasks "
            "WHERE workflow_id = $1 ORDER BY created_at, key", __import__("uuid").UUID(wf_id))
    finally:
        await conn.close()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252 (no ₹, emoji)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="LLM_PROVIDER=fake (no Gemini calls)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--goal", default=DEFAULT_GOAL)
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    banner(f"1. Start server ({'FAKE' if args.fake else 'GEMINI'} provider)")
    proc, log = start_server(args.port, args.fake, "crash_demo_run1")
    with httpx.Client(base_url=base, timeout=30) as c:
        r = c.post("/workflows", json={"name": "crash demo", "goal": args.goal, "run": True})
        r.raise_for_status()
        wf_id = r.json()["id"]
        print(f"Workflow {wf_id} accepted (run=true)")

        banner("2. Wait for the plan, then arm a crash on the writer task")
        t0 = time.monotonic()
        while True:
            wf = c.get(f"/workflows/{wf_id}").json()
            if wf["tasks"]:
                break
            if wf["status"] == "FAILED" or time.monotonic() - t0 > args.timeout:
                print(f"Planning failed: {wf.get('error')}")
                proc.kill()
                return 1
            time.sleep(0.5)
        writer = next((t["key"] for t in wf["tasks"] if t["agent_type"] == "writer"), wf["tasks"][-1]["key"])
        c.post("/dev/chaos", json={"workflow_id": wf_id, "task_key": writer, "mode": "crash_process"}
               ).raise_for_status()
        print(f"Plan has {len(wf['tasks'])} tasks; crash_process armed on '{writer}'")

        banner("3. Run until the server crashes")
        last = None
        while proc.poll() is None:
            try:
                wf = c.get(f"/workflows/{wf_id}").json()
            except httpx.HTTPError:
                time.sleep(0.2)
                continue
            snap = [(t["key"], t["status"]) for t in wf["tasks"]]
            if snap != last:
                show(wf, t0)
                last = snap
            if wf["status"] in TERMINAL:
                print(f"Workflow ended {wf['status']} before the crash fired")
                proc.kill()
                return 1
            time.sleep(0.3)
        log.close()
        print(f"\n*** SERVER CRASHED (exit code {proc.returncode}) ***", flush=True)

    banner("4. Database state while the server is down")
    rows = asyncio.run(db_snapshot(wf_id))
    for r in rows:
        print(f"   {r['key']:<26} {r['status']:<10} attempts={r['attempt_count']}  lease_owner={r['lease_owner']}")
    succeeded_before = {r["key"] for r in rows if r["status"] == "SUCCEEDED"}
    running_before = {r["key"] for r in rows if r["status"] == "RUNNING"}
    print(f"Preserved (SUCCEEDED): {sorted(succeeded_before)}\nOrphaned (RUNNING):    {sorted(running_before)}")

    banner("5. Restart the server")
    proc, log = start_server(args.port, args.fake, "crash_demo_run2")
    try:
        with httpx.Client(base_url=base, timeout=30) as c:
            events = c.get(f"/workflows/{wf_id}/events").json()
            rec = next((e for e in events if e["event_type"] == "engine.recovered"), None)
            print(f"engine.recovered: {rec['payload'] if rec else 'NOT FOUND'}")

            banner("6. Wait for completion")
            last = None
            while True:
                wf = c.get(f"/workflows/{wf_id}").json()
                snap = [(t["key"], t["status"]) for t in wf["tasks"]]
                if snap != last:
                    show(wf, t0)
                    last = snap
                if wf["status"] in TERMINAL or time.monotonic() - t0 > args.timeout:
                    break
                time.sleep(0.5)

            banner("7. Attempts report")
            ok = wf["status"] == "COMPLETED" and rec is not None and bool(running_before)
            for t in wf["tasks"]:
                rows = c.get(f"/workflows/{wf_id}/tasks/{t['key']}/attempts").json()
                print(f"   {t['key']:<26} " + ", ".join(
                    f"#{a['attempt_number']} {a['outcome']}" + (f"({a['error_type']})" if a["error_type"] else "")
                    for a in rows))
                if t["key"] in succeeded_before and len(rows) != 1:
                    ok = False
                    print(f"      ^ FAIL: completed before the crash but ran {len(rows)} times")
                if t["key"] in running_before and not (
                        len(rows) >= 2 and rows[0]["outcome"] == "worker_lost" and rows[-1]["outcome"] == "succeeded"):
                    ok = False
                    print("      ^ FAIL: expected worker_lost then succeeded")
            banner("8. Timeline (GET /workflows/{id}/timeline)")
            tl = c.get(f"/workflows/{wf_id}/timeline").json()
            for e in tl["entries"]:
                if e["event_type"] not in ("task.created", "task.ready"):  # keep the story short
                    print(f"   +{e['offset_s']:6.1f}s  {e['text']}")
            print(f"   summary: {tl['summary']}")
            print(f"\nWorkflow {wf['status']} - {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 1
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
