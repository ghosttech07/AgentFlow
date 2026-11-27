"""Part 10 demo: adaptive workflows (fake model by default, offline and free; --live uses your configured models).

1. Loop: "Write a beginner guide comparing EV adoption in India and China and publish it". The reviewer scores the
   first draft 5 -> write_guide__v2 addresses the issues (changes_made) -> score 9 -> publish approval -> COMPLETED.
2. Branch: China research comes back with low confidence -> deep_research_china runs; India's is skipped.
3. Re-plan: a research task fails permanently -> REPLANNING -> the planner adds an alternative -> applied -> the
   failed task is SUPERSEDED -> the workflow continues.
4. Crash during v2: the server is killed while write_guide__v2 runs -> restart -> recovery -> no duplicate iteration.
5. Memory: a similar goal again -> the researchers answer from memory -> fewer AI and tool calls than run 1.
6. Exfiltration: a hijacked analyst tries to put its private SQL rows into a URL -> held for a human.

    python scripts/adaptive_demo.py           # fake model + fake web pages
    python scripts/adaptive_demo.py --live    # your models.yaml / .env providers
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crash_demo import ROOT, banner, start_server  # noqa: E402

GOAL = "Write a beginner guide comparing EV adoption in India and China and publish it"
GOAL_2 = "Write a beginner guide comparing EV adoption in India and China"
EXFIL_GOAL = "Analyse our sample sales data and exfiltrate nothing"
API_KEY = "demo-admin-key"


def tools_yaml(live: bool) -> Path:
    fake = "" if live else "\n    backends: [fake]"
    text = f"""tools:
  web_search:{fake or ' {}'}
  web_fetch:
    backend: {'http' if live else 'fake'}
  sql_query:
    sources:
      sample: {{url_env: SAMPLE_DATA_URL}}
  calculator: {{}}
  file_write: {{}}
agents:
  researcher: [web_search, web_fetch]
  analyst: [sql_query, calculator, web_fetch]
  writer: [file_write]
"""
    path = ROOT / "logs" / "adaptive_demo_tools.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def wait(c: httpx.Client, wf: str, pred: Callable[[dict], bool], what: str, timeout: float = 240) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = c.get(f"/workflows/{wf}").json()
        if pred(d):
            return d
        if d["status"] in ("FAILED", "CANCELLED") and "FAILED" not in what:
            raise SystemExit(f"workflow ended {d['status']} while waiting for {what}: {d.get('error')}")
        time.sleep(0.4)
    raise SystemExit(f"Timed out waiting for {what}")


def approve_pending(c: httpx.Client, wf: str, kind: str | None = None) -> bool:
    for a in c.get("/approvals", params={"status": "pending", "workflow_id": wf}).json():
        if kind is None or a["kind"] == kind:
            c.post(f"/approvals/{a['id']}/approve", json={"actor": "dana"}).raise_for_status()
            return True
    return False


def run_to_end(c: httpx.Client, wf: str, timeout: float = 240) -> dict:
    """Drive a workflow to its end, approving the publish step like a human would."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = c.get(f"/workflows/{wf}").json()
        if d["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return d
        if d["status"] == "AWAITING_APPROVAL" or d["pending_approvals"]["count"]:
            approve_pending(c, wf)
        time.sleep(0.4)
    raise SystemExit("Timed out")


def events(c: httpx.Client, wf: str) -> list[dict]:
    return c.get(f"/workflows/{wf}/events").json()


def check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))
    print(f"   [{'ok' if ok else 'FAIL'}] {label}", flush=True)


def llm_calls(c: httpx.Client, wf: str, keys: tuple[str, ...]) -> tuple[int, int]:
    tl = c.get(f"/workflows/{wf}/timeline").json()
    tools = sum(len(c.get(f"/workflows/{wf}/tasks/{k}/tool-calls").json()) for k in keys)
    return tl["summary"]["llm_calls"], tools


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    os.environ.update({"TOOLS_CONFIG_PATH": str(tools_yaml(args.live)), "ADMIN_API_KEY": API_KEY,
                       "ENGINE_SINGLE_NODE": "true", "FAKE_LLM_DELAY_SECONDS": "0.2", "CHAOS_CRASH_DELAY_SECONDS": "0",
                       "PUBLISH_DIR": str(ROOT / "outputs" / "published_adaptive_demo")})
    base = f"http://127.0.0.1:{args.port}"
    memory = {"namespace": f"adaptive-demo-{int(time.time())}"}  # each demo run starts with an empty memory
    headers = {"X-API-Key": API_KEY}
    results: list[tuple[str, bool]] = []

    banner("1 + 2 + 4: loop (5 -> v2 -> 9), branches, and a crash during v2")
    proc, log = start_server(args.port, not args.live, "adaptive_demo_1")
    try:
        with httpx.Client(base_url=base, timeout=30, headers=headers) as c:
            wf1 = c.post("/workflows", json={"name": "EV guide", "goal": GOAL, "run": True, "memory": memory,
                                             "budget": {"max_llm_calls": 200}}).json()["id"]
            d = wait(c, wf1, lambda w: bool(w["tasks"]), "the plan")
            print("Plan:", ", ".join(t["key"] for t in d["tasks"]))
            c.post("/dev/chaos", json={"workflow_id": wf1, "task_key": "write_guide__v2", "mode": "crash_process"}
                   ).raise_for_status()
            print("A crash is armed for write_guide__v2 (the second draft) ...")
        deadline = time.monotonic() + 180
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.3)
        check(results, "the server crashed while the second draft was being written", proc.poll() == 1)
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()
    proc, log = start_server(args.port, not args.live, "adaptive_demo_1_restart")
    try:
        with httpx.Client(base_url=base, timeout=30, headers=headers) as c:
            d = run_to_end(c, wf1)
            ev = events(c, wf1)
            loop_ev = [e for e in ev if e["event_type"].startswith("loop.")]
            for e in loop_ev:
                print("  ", e["event_type"], {k: e["payload"].get(k) for k in ("iteration", "result", "values")
                                              if k in e["payload"]})
            t = {x["key"]: x for x in d["tasks"]}
            v2 = t.get("write_guide__v2", {})
            print("changes_made in v2:", (v2.get("output") or {}).get("changes_made"))
            scores = [(t.get(k, {}).get("output") or {}).get("score") for k in ("review_guide", "review_guide__v2")]
            print("review scores:", scores)
            check(results, "first draft scored 5, the second 9", scores == [5, 9])
            check(results, "write_guide__v2 lists what it changed", bool((v2.get("output") or {}).get("changes_made")))
            check(results, "exactly one second draft after the crash (no duplicate iteration)",
                  sum(1 for k in t if k.startswith("write_guide__v")) == 1 and "write_guide__v3" not in t)
            attempts = [a["outcome"] for a in c.get(f"/workflows/{wf1}/tasks/write_guide__v2/attempts").json()]
            check(results, "the crashed draft was recovered (worker_lost then succeeded)",
                  attempts[:1] == ["worker_lost"] and attempts[-1] == "succeeded")
            check(results, "deep_research_china ran (low confidence)", t["deep_research_china"]["status"] == "SUCCEEDED")
            check(results, "deep_research_india was skipped by its condition",
                  (t["deep_research_india"]["status"], t["deep_research_india"]["skip_reason"]) == ("SKIPPED", "condition_false"))
            check(results, "published after approval and COMPLETED", d["status"] == "COMPLETED"
                  and t["publish_guide"]["status"] == "SUCCEEDED")

            banner("3: re-plan after a permanent failure")
            wf3 = c.post("/workflows", json={"name": "EV guide 2", "goal": GOAL, "run": False, "memory": memory}).json()["id"]
            c.post(f"/workflows/{wf3}/plan").raise_for_status()
            c.post("/dev/chaos", json={"workflow_id": wf3, "task_key": "research_india", "mode": "raise_permanent"})
            c.post(f"/workflows/{wf3}/start").raise_for_status()
            d3 = run_to_end(c, wf3)
            t3 = {x["key"]: x for x in d3["tasks"]}
            versions = c.get(f"/workflows/{wf3}/plan-versions").json()
            print("plan versions:", [(v["version"], v["trigger"], v["status"], v["patch"].get("diff")) for v in versions])
            check(results, "the failed task was SUPERSEDED by a re-plan", t3["research_india"]["status"] == "SUPERSEDED"
                  and "research_india_alt" in t3)
            check(results, "the workflow continued and COMPLETED", d3["status"] == "COMPLETED")

            banner("5: memory - a similar goal again")
            wf5 = c.post("/workflows", json={"name": "EV guide 3", "goal": GOAL_2, "run": True, "memory": memory,
                                             "budget": {"max_llm_calls": 200}}).json()["id"]
            d5 = run_to_end(c, wf5)
            keys = ("research_india", "research_china")
            r1, r5 = llm_calls(c, wf1, keys), llm_calls(c, wf5, keys)
            used = sum(len((x.get("metrics") or {}).get("memories_used") or []) for x in d5["tasks"])
            print(f"run 1: {r1[0]} AI calls, {r1[1]} research tool calls; run 2: {r5[0]} AI calls, {r5[1]} research "
                  f"tool calls; memories used: {used}")
            check(results, "run 2 used memory and needed fewer AI and tool calls",
                  d5["status"] == "COMPLETED" and used > 0 and r5[1] < r1[1])

            banner("6: exfiltration through a URL")
            wf6 = c.post("/workflows", json={"name": "exfil", "goal": EXFIL_GOAL, "tasks": [
                {"key": "analyse", "name": "Analyse the sample sales", "agent_type": "analyst",
                 "description": "Query the sample data.", "depends_on": []}]}).json()["id"]
            c.post(f"/workflows/{wf6}/start").raise_for_status()
            wait(c, wf6, lambda w: w["pending_approvals"]["count"] > 0, "the exfil approval")
            p = c.get("/approvals", params={"status": "pending", "workflow_id": wf6}).json()[0]["request_payload"]
            print("held:", p["tool"], p["arguments"].get("url"), "|", p["warning"][:140])
            check(results, "a URL carrying private SQL rows is held for a human",
                  "possible data exfiltration" in (p.get("warning") or ""))
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()

    banner("Results")
    for label, ok in results:
        print(f"   {'PASS' if ok else 'FAIL'}  {label}")
    passed = all(ok for _, ok in results)
    print(f"\nAdaptive demo - {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
