"""Part 11 demo: users and roles, team isolation, policy-as-code, two approvers, four eyes (fake model, offline).

1. The admin key creates two teams and five users (each key is shown once; only its hash is stored).
2. Isolation: marketing cannot see, list or cancel research's workflow (404, not 403).
3. Roles: a viewer cannot cancel (403); the actor recorded is the signed-in user, not what the body claims.
4. Policy "publish_needs_two_approvers": an operator may not approve (403); ann approves (1 of 2); ann again (409);
   bob approves (2 of 2) -> published -> COMPLETED.
5. Four eyes: ann creates a publish workflow herself and cannot approve it (403).
6. Safety floor: a policy file that tries `require_approval: false` is refused at load.

    python scripts/governance_demo.py

The demo's users and teams are removed from the database at the start and the end, so other demos and the
dashboard keep working without keys.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crash_demo import ROOT, banner, start_server  # noqa: E402

sys.path.insert(0, str(ROOT))
ADMIN = {"X-API-Key": "demo-admin-key"}
PREFIX = "gd-"
POLICY = """rules:
  - name: publish_needs_two_approvers
    when: {agent_type: publisher}
    approvals: 2
    approver_role: approver
"""


async def cleanup() -> None:
    """Remove the demo's users and teams (and detach its workflows) so no keys are required afterwards."""
    import asyncpg

    from app.config import get_settings

    conn = await asyncpg.connect(get_settings().database_url.replace("+asyncpg", ""))
    try:
        await conn.execute("DELETE FROM users WHERE name LIKE $1", PREFIX + "%")
        await conn.execute("UPDATE workflows SET team_id = NULL WHERE team_id IN "
                           "(SELECT id FROM teams WHERE name LIKE $1)", PREFIX + "%")
        await conn.execute("DELETE FROM teams WHERE name LIKE $1", PREFIX + "%")
    finally:
        await conn.close()


def check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))
    print(f"   [{'ok' if ok else 'FAIL'}] {label}", flush=True)


def wait(c: httpx.Client, wf: str, h: dict, pred, what: str, timeout: float = 120) -> dict:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = c.get(f"/workflows/{wf}", headers=h).json()
        if pred(d):
            return d
        time.sleep(0.4)
    raise SystemExit(f"Timed out waiting for {what}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    port = 8765
    policy_path = Path(tempfile.gettempdir()) / "agentflow_governance_demo_policies.yaml"
    policy_path.write_text(POLICY, encoding="utf-8")
    os.environ.update({"ADMIN_API_KEY": "demo-admin-key", "POLICIES_CONFIG_PATH": str(policy_path),
                       "FAKE_LLM_DELAY_SECONDS": "0.2", "PUBLISH_DIR": str(ROOT / "outputs" / "published_gov_demo")})
    asyncio.run(cleanup())
    results: list[tuple[str, bool]] = []
    proc, log = start_server(port, True, "governance_demo")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
            banner("1: teams and users (the admin key bootstraps)")
            for t in ("research", "marketing"):
                c.post("/admin/teams", json={"name": PREFIX + t}, headers=ADMIN).raise_for_status()
            k: dict[str, dict] = {}
            for name, role, team in (("olga", "operator", "research"), ("ann", "approver", "research"),
                                     ("bob", "approver", "research"), ("vic", "viewer", "research"),
                                     ("mia", "operator", "marketing")):
                r = c.post("/admin/users", json={"name": PREFIX + name, "role": role, "team": PREFIX + team}, headers=ADMIN)
                r.raise_for_status()
                k[name] = {"X-API-Key": r.json()["api_key"]}
                print(f"   {PREFIX + name:<9} {role:<9} team {PREFIX + team:<12} key af_...{r.json()['api_key'][-4:]}")
            listed = c.get("/admin/users", headers=ADMIN).json()
            check(results, "keys are never listed (only shown once at creation)",
                  all("api_key" not in u and "api_key_hash" not in u for u in listed))
            check(results, "without a key the API answers 401", c.get("/workflows").status_code == 401)

            banner("2 + 3: isolation and roles")
            goal = "Write a short EV note and publish it"
            wf = c.post("/workflows", json={"name": "EV note", "goal": goal, "run": True}, headers=k["olga"]).json()["id"]
            wait(c, wf, k["olga"], lambda d: d["pending_approvals"]["count"] > 0, "the publish approval")
            check(results, "marketing cannot see research's workflow (404)",
                  c.get(f"/workflows/{wf}", headers=k["mia"]).status_code == 404)
            check(results, "marketing's list and approvals do not show it",
                  wf not in [w["id"] for w in c.get("/workflows", headers=k["mia"]).json()["items"]]
                  and c.get("/approvals", headers=k["mia"]).json() == [])
            check(results, "a viewer cannot cancel (403)",
                  c.post(f"/workflows/{wf}/cancel", json={}, headers=k["vic"]).status_code == 403)

            banner("4: policy publish_needs_two_approvers")
            a = c.get("/approvals", params={"status": "pending", "workflow_id": wf}, headers=k["ann"]).json()[0]
            print(f"   approval needs {a['required_approvals']} approvers (role {a['approver_role']}), "
                  f"policy {a['policy_rules']}")
            url = f"/approvals/{a['id']}/approve"
            check(results, "an operator cannot approve (403)", c.post(url, json={}, headers=k["olga"]).status_code == 403)
            r = c.post(url, json={"actor": "someone-else", "comment": "looks right"}, headers=k["ann"]).json()
            check(results, "ann approves: 1 of 2, recorded as ann", r["status"] == "pending"
                  and [v["voter"] for v in r["votes"]] == [PREFIX + "ann"])
            check(results, "ann cannot approve twice (409)", c.post(url, json={}, headers=k["ann"]).status_code == 409)
            r = c.post(url, json={}, headers=k["bob"]).json()
            check(results, "bob approves: 2 of 2 -> approved", r["status"] == "approved")
            d = wait(c, wf, k["olga"], lambda d: d["status"] in ("COMPLETED", "FAILED"), "completion")
            check(results, "published and COMPLETED", d["status"] == "COMPLETED")
            lines = [e["text"] for e in c.get(f"/workflows/{wf}/timeline", headers=k["olga"]).json()["entries"]
                     if "approv" in e["text"].lower()]
            for line in lines:
                print("   timeline:", line)

            banner("5: four eyes")
            wf2 = c.post("/workflows", json={"name": "EV note 2", "goal": goal, "run": True}, headers=k["ann"]).json()["id"]
            wait(c, wf2, k["ann"], lambda d: d["pending_approvals"]["count"] > 0, "the second approval")
            a2 = c.get("/approvals", params={"status": "pending", "workflow_id": wf2}, headers=k["ann"]).json()[0]
            r = c.post(f"/approvals/{a2['id']}/approve", json={}, headers=k["ann"])
            print("  ", r.json()["error"]["message"])
            check(results, "the creator cannot approve their own workflow (403)", r.status_code == 403)
            c.post(f"/workflows/{wf2}/cancel", json={"reason": "demo"}, headers=k["olga"])
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()
        asyncio.run(cleanup())

    banner("6: safety floor")
    from app.governance.policy import PolicyConfigError, load_policies

    bad = Path(tempfile.gettempdir()) / "agentflow_bad_policy.yaml"
    bad.write_text("rules:\n  - name: no_review\n    when: {agent_type: publisher}\n    require_approval: false\n",
                   encoding="utf-8")
    try:
        load_policies(bad)
        refused = False
    except PolicyConfigError:
        refused = True
    check(results, "a policy that tries to remove an approval is refused at startup", refused)

    banner("Results")
    for label, ok in results:
        print(f"   {'PASS' if ok else 'FAIL'}  {label}")
    passed = all(ok for _, ok in results)
    print(f"\nGovernance demo - {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
