"""Part 11: users, roles, tenant isolation, policy-as-code, multi-approver approvals, safety floor."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.governance import policy as policy_mod
from app.governance.policy import Policies, PolicyConfigError, Rule, load_policies
from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import task

H = dict[str, str]


async def setup_org(client: AsyncClient) -> dict[str, H]:
    """Bootstrap in open dev mode (no admin key, no users): a global admin first (that closes the open mode), then
    two teams and five users with the admin's key. Returns name -> headers."""
    r = await client.post("/admin/users", json={"name": "root", "role": "admin"})
    assert r.status_code == 201, r.text
    root = {"X-API-Key": r.json()["api_key"]}
    for t in ("research", "marketing"):
        assert (await client.post("/admin/teams", json={"name": t}, headers=root)).status_code == 201
    keys: dict[str, H] = {"root": root}
    for name, role, team in (("olga", "operator", "research"), ("ann", "approver", "research"),
                             ("bob", "approver", "research"), ("vic", "viewer", "research"),
                             ("mia", "operator", "marketing")):
        r = await client.post("/admin/users", json={"name": name, "role": role, "team": team}, headers=root)
        assert r.status_code == 201, r.text
        keys[name] = {"X-API-Key": r.json()["api_key"]}
    return keys


def use_policies(monkeypatch: pytest.MonkeyPatch, *rules: dict) -> None:
    pols = Policies([Rule.model_validate(r) for r in rules])
    monkeypatch.setattr(policy_mod, "get_policies", lambda: pols)


async def create(client: AsyncClient, h: H, tasks: list[dict], name: str = "w") -> str:
    r = await client.post("/workflows", json={"name": name, "goal": "g", "tasks": tasks}, headers=h)
    assert r.status_code == 201, r.text
    wf = r.json()["id"]
    assert (await client.post(f"/workflows/{wf}/start", headers=h)).status_code == 202
    return wf


# ------------------------------------------------------------------------------ auth + roles
async def test_keys_are_shown_once_and_never_listed(client: AsyncClient) -> None:
    keys = await setup_org(client)
    me = (await client.get("/me", headers=keys["ann"])).json()
    assert me == {"kind": "user", "name": "ann", "role": "approver", "team": "research", "sees_all_teams": False}
    users = (await client.get("/admin/users", headers=keys["olga"])).json()  # operator: not an admin
    assert users["error"]["code"] == "forbidden"


async def test_once_users_exist_a_key_is_required(client: AsyncClient) -> None:
    keys = await setup_org(client)
    assert (await client.get("/workflows")).status_code == 401
    assert (await client.get("/workflows", headers={"X-API-Key": "af_wrong"})).status_code == 401
    assert (await client.get("/workflows", headers=keys["vic"])).status_code == 200
    r = await client.post("/workflows", json={"name": "w", "goal": "g"}, headers=keys["vic"])
    assert r.status_code == 403  # a viewer cannot create
    assert (await client.get("/health")).status_code == 200  # health stays public


async def test_roles_gate_controls_and_the_actor_is_the_user(client: AsyncClient) -> None:
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("a")])
    assert (await client.post(f"/workflows/{wf}/pause", json={}, headers=keys["vic"])).status_code == 403
    r = await client.post(f"/workflows/{wf}/pause", json={"actor": "someone-else"}, headers=keys["olga"])
    assert r.status_code == 200
    ev = [e for e in (await client.get(f"/workflows/{wf}/events", headers=keys["olga"])).json()
          if e["event_type"] == "workflow.paused"]
    assert ev[0]["payload"]["actor"] == "olga"  # the body cannot impersonate


async def test_a_disabled_user_and_a_rotated_key_stop_working(client: AsyncClient) -> None:
    keys = await setup_org(client)
    users = {u["name"]: u for u in (await client.get("/admin/users", headers=keys["root"])).json()}
    assert "api_key" not in users["ann"] and "api_key_hash" not in users["ann"]
    assert (await client.post(f"/admin/users/{users['vic']['id']}/disable", headers=keys["root"])).status_code == 200
    assert (await client.get("/workflows", headers=keys["vic"])).status_code == 401
    r = await client.post(f"/admin/users/{users['ann']['id']}/rotate-key", headers=keys["root"])
    assert (await client.get("/workflows", headers=keys["ann"])).status_code == 401
    assert (await client.get("/workflows", headers={"X-API-Key": r.json()["api_key"]})).status_code == 200
    r = await client.post("/admin/users", json={"name": "root", "role": "admin"}, headers={})
    assert r.status_code == 401  # users exist: the bootstrap path is closed
    r = await client.post("/admin/users", json={"name": "lead", "role": "admin", "team": "research"},
                          headers=keys["olga"])
    assert r.status_code == 403


# ------------------------------------------------------------------------------ tenant isolation
async def test_teams_cannot_see_each_other(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("p", "publisher")])
    await make_engine(FakeLLMClient()).run_until_idle()  # publisher waits for an approval
    mia = keys["mia"]
    for url in (f"/workflows/{wf}", f"/workflows/{wf}/events", f"/workflows/{wf}/timeline", f"/workflows/{wf}/tasks"):
        assert (await client.get(url, headers=mia)).status_code == 404, url
    assert (await client.post(f"/workflows/{wf}/cancel", json={}, headers=mia)).status_code == 404
    assert wf not in [w["id"] for w in (await client.get("/workflows", headers=mia)).json()["items"]]
    assert (await client.get("/approvals", headers=mia)).json() == []
    approval = (await client.get("/approvals", headers=keys["ann"])).json()[0]["id"]
    assert (await client.get(f"/approvals/{approval}", headers=mia)).status_code == 404
    assert wf in [w["id"] for w in (await client.get("/workflows", headers=keys["vic"])).json()["items"]]


async def test_team_memory_namespaces_are_separate(client: AsyncClient) -> None:
    keys = await setup_org(client)
    r = await client.post("/workflows", json={"name": "w", "goal": "g", "memory": {"namespace": "default"}},
                          headers=keys["olga"])
    assert r.json()["context"]["memory"]["namespace"].startswith("t")
    r2 = await client.post("/workflows", json={"name": "w", "goal": "g", "memory": {
        "namespace": r.json()["context"]["memory"]["namespace"]}}, headers=keys["mia"])
    assert r2.json()["context"]["memory"]["namespace"] != r.json()["context"]["memory"]["namespace"]


# ------------------------------------------------------------------------------ policy + multi-approver
TWO = {"name": "publish_needs_two", "when": {"agent_type": "publisher"}, "approvals": 2, "approver_role": "approver"}


async def pending(client: AsyncClient, h: H, wf: str) -> dict:
    rows = (await client.get(f"/approvals?status=pending&workflow_id={wf}", headers=h)).json()
    assert len(rows) == 1, rows
    return rows[0]


async def test_two_distinct_approvers_are_needed(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    use_policies(monkeypatch, TWO)
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("w", "writer"), task("p", "publisher", ["w"])])
    engine = make_engine(FakeLLMClient())
    await engine.run_until_idle()
    a = await pending(client, keys["ann"], wf)
    assert (a["required_approvals"], a["approver_role"], a["policy_rules"]) == (2, "approver", ["publish_needs_two"])
    url = f"/approvals/{a['id']}/approve"
    assert (await client.post(url, json={}, headers=keys["olga"])).status_code == 403  # operator: wrong role
    r = await client.post(url, json={"comment": "ok"}, headers=keys["ann"])
    assert r.status_code == 200 and r.json()["status"] == "pending" and len(r.json()["votes"]) == 1
    assert (await client.post(url, json={}, headers=keys["ann"])).status_code == 409  # same person twice
    r = await client.post(url, json={}, headers=keys["bob"])
    assert r.json()["status"] == "approved" and [v["voter"] for v in r.json()["votes"]] == ["ann", "bob"]
    await engine.run_until_idle()
    d = (await client.get(f"/workflows/{wf}", headers=keys["olga"])).json()
    assert d["status"] == "COMPLETED"


async def test_four_eyes_creator_cannot_approve(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    use_policies(monkeypatch, TWO)
    keys = await setup_org(client)
    # ann (an approver) creates the workflow herself, so she may not approve it
    wf2 = (await client.post("/workflows", json={"name": "w2", "goal": "g", "tasks": [task("p", "publisher")]},
                             headers=keys["ann"])).json()["id"]
    await client.post(f"/workflows/{wf2}/start", headers=keys["ann"])
    await make_engine(FakeLLMClient()).run_until_idle()
    a = await pending(client, keys["ann"], wf2)
    r = await client.post(f"/approvals/{a['id']}/approve", json={}, headers=keys["ann"])
    assert r.status_code == 403 and "Four-eyes" in r.json()["error"]["message"]


async def test_an_edit_resets_earlier_votes(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    use_policies(monkeypatch, TWO)
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("p", "publisher")])
    await make_engine(FakeLLMClient()).run_until_idle()
    a = await pending(client, keys["ann"], wf)
    url = f"/approvals/{a['id']}/approve"
    await client.post(url, json={}, headers=keys["ann"])
    r = await client.post(url, json={"input_overrides": {"title": "Edited"}}, headers=keys["bob"])
    assert r.json()["status"] == "pending" and [v["voter"] for v in r.json()["votes"]] == ["bob"]
    r = await client.post(url, json={}, headers=keys["ann"])  # ann now approves the edited content
    assert r.json()["status"] == "approved"
    t = (await client.get(f"/workflows/{wf}/tasks/p", headers=keys["olga"])).json()
    assert t["input"]["title"] == "Edited"


async def test_one_veto_rejects(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    use_policies(monkeypatch, TWO)
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("p", "publisher")])
    await make_engine(FakeLLMClient()).run_until_idle()
    a = await pending(client, keys["ann"], wf)
    await client.post(f"/approvals/{a['id']}/approve", json={}, headers=keys["ann"])
    r = await client.post(f"/approvals/{a['id']}/reject", json={"reason": "no", "on_reject": "skip_task"},
                          headers=keys["bob"])
    assert r.json()["status"] == "rejected" and r.json()["decided_by"] == "bob"


async def test_a_policy_can_require_approval_for_a_task(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    use_policies(monkeypatch, {"name": "review_research", "when": {"task_key": "research_*", "team": "research"},
                               "require_approval": True})
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("research_x"), task("other")])
    await make_engine(FakeLLMClient()).run_until_idle()
    t = {x["key"]: x for x in (await client.get(f"/workflows/{wf}/tasks", headers=keys["olga"])).json()}
    assert t["research_x"]["status"] == "AWAITING_APPROVAL" and t["other"]["status"] == "SUCCEEDED"
    wf2 = await create(client, keys["mia"], [task("research_x")])  # another team: the rule does not apply
    await make_engine(FakeLLMClient()).run_until_idle()
    t2 = (await client.get(f"/workflows/{wf2}/tasks/research_x", headers=keys["mia"])).json()
    assert t2["status"] == "SUCCEEDED"


# ------------------------------------------------------------------------------ policy file + safety floor
def test_policy_file_can_only_add_requirements(tmp_path) -> None:  # noqa: ANN001
    bad = tmp_path / "p.yaml"
    bad.write_text("rules:\n  - name: off\n    when: {agent_type: publisher}\n    require_approval: false\n")
    with pytest.raises(PolicyConfigError):
        load_policies(bad)
    bad.write_text("rules:\n  - {name: a, unknown_field: 1}\n")
    with pytest.raises(PolicyConfigError):
        load_policies(bad)
    ok = tmp_path / "ok.yaml"
    ok.write_text("rules:\n  - name: low\n    when: {tool: send_email}\n    approver_role: viewer\n")
    assert load_policies(ok).rules[0].approver_role == "approver"  # floor: viewers can never approve
    assert load_policies(tmp_path / "missing.yaml").rules == []


def test_strictest_rule_wins_and_recipient_domains() -> None:
    pols = Policies([Rule.model_validate(r) for r in (
        {"name": "a", "when": {"tool": "send_email"}, "approvals": 2},
        {"name": "b", "when": {"tool": "send_email", "recipient_not_in_domains": ["example.com"]},
         "approvals": 3, "approver_role": "admin"},
        {"name": "c", "when": {"tool": "webhook_post", "team": "marketing"}, "deny": True})])
    inside = pols.evaluate(tool="send_email", recipients=["x@example.com"])
    outside = pols.evaluate(tool="send_email", recipients=["x@example.com", "y@evil.com"])
    assert (inside.approvals, inside.rules) == (2, ["a"])
    assert (outside.approvals, outside.approver_role, outside.rules) == (3, "admin", ["a", "b"])
    assert pols.evaluate(tool="webhook_post", team="marketing").deny
    assert not pols.evaluate(tool="webhook_post", team="research").deny


async def test_legacy_open_mode_is_unchanged(client: AsyncClient) -> None:
    wf = (await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": [task("a")]})).json()["id"]
    assert (await client.post(f"/workflows/{wf}/pause", json={"actor": "dana"})).status_code == 409  # not running
    assert (await client.get(f"/workflows/{wf}")).status_code == 200
    assert (await client.get("/me")).json()["kind"] == "open"


async def test_a_policy_can_deny_a_tool_call(client: AsyncClient, make_engine, session_factory, monkeypatch,  # noqa: ANN001
                                             tmp_path) -> None:  # noqa: ANN001
    from app.agents.writer import WriterAgent
    from app.llm.fake_client import FakeScript
    from tests.engine_helpers import match, started_workflow
    from tests.test_budgets import fake_router
    from tests.tool_helpers import BASE_AGENTS, BASE_TOOLS, call, final, tool_rows, use_tools

    monkeypatch.setattr(WriterAgent, "allowed_tools", ("send_email",))
    use_tools(monkeypatch, tools={**BASE_TOOLS, "send_email": {**BASE_TOOLS["send_email"], "approval": "never"}},
              agents={**BASE_AGENTS, "writer": ["send_email"]})
    use_policies(monkeypatch, {"name": "no_email", "when": {"tool": "send_email"}, "deny": True})
    report = {"title": "t", "content": "c", "word_count": 1}
    router, _ = fake_router(session_factory, llm=FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=[
        call("send_email", to=["a@example.com"], subject="s", body="b"), final(report)])]))
    wf = await started_workflow(client, [task("w", "writer")])
    await make_engine(router, outbox_dir=tmp_path / "outbox").run_until_idle()
    rows = await tool_rows(session_factory, wf)
    assert [(r.tool, r.status) for r in rows] == [("send_email", "rejected_invalid")]
    assert "denied by policy (no_email)" in rows[0].error
    assert not list((tmp_path / "outbox").glob("*.eml"))  # nothing was sent


# ------------------------------------------------------------------------------ final review fixes (C1, I1-I4)
async def test_history_and_asks_are_per_team(client: AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    from app.services import ask_service

    async def fake_ask(question, llm, settings):  # noqa: ANN001, ANN202
        return {"answer": "a", "key_points": [], "sources": []}

    monkeypatch.setattr(ask_service, "ask", fake_ask)
    keys = await setup_org(client)
    wf = await create(client, keys["olga"], [task("a")])
    q = (await client.post("/ask", json={"question": "research question"}, headers=keys["olga"])).json()["id"]
    assert (await client.get("/history")).status_code == 401
    assert (await client.post("/ask", json={"question": "free money?"})).status_code == 401
    mine = {i["id"] for i in (await client.get("/history", headers=keys["vic"])).json()["items"]}
    theirs = {i["id"] for i in (await client.get("/history", headers=keys["mia"])).json()["items"]}
    assert {wf, q} <= mine and not ({wf, q} & theirs)
    assert (await client.get(f"/asks/{q}", headers=keys["mia"])).status_code == 404


async def test_ops_endpoints_need_a_key_once_users_exist(client: AsyncClient) -> None:
    keys = await setup_org(client)
    for method, url, body in (("POST", "/plans/preview", {"goal": "g"}), ("GET", "/dev/chaos", None),
                              ("GET", "/metrics/summary", None), ("GET", "/engine/status", None)):
        r = await client.request(method, url, json=body)
        assert r.status_code == 401, (url, r.status_code)
    wf = await create(client, keys["olga"], [task("a")])
    r = await client.post("/dev/chaos", json={"workflow_id": wf, "task_key": "a", "mode": "hang"}, headers=keys["mia"])
    assert r.status_code in (403, 404)  # never on another team's workflow
    assert (await client.get("/metrics/summary", headers=keys["mia"])).status_code == 403  # cross-team totals


async def test_a_long_team_namespace_is_rejected_up_front(client: AsyncClient) -> None:
    keys = await setup_org(client)
    r = await client.post("/workflows", json={"name": "w", "goal": "g", "memory": {"namespace": "n" * 60}},
                          headers=keys["olga"])
    assert r.status_code in (201, 422)
    if r.status_code == 201:
        assert len(r.json()["context"]["memory"]["namespace"]) <= 64


def test_task_rules_do_not_apply_to_tool_calls_and_tool_deny_needs_a_tool() -> None:
    pols = Policies([Rule.model_validate({"name": "r", "when": {"agent_type": "researcher"}, "approvals": 2})])
    assert pols.evaluate(scope="tool", agent_type="researcher", tool="web_search").rules == []
    assert pols.evaluate(scope="task", agent_type="researcher").approvals == 2
    with pytest.raises(ValueError):
        Rule.model_validate({"name": "x", "when": {"agent_type": "writer"}, "deny": True})


async def test_edited_arguments_are_checked_against_the_policy(client: AsyncClient, make_engine, session_factory,  # noqa: ANN001
                                                                monkeypatch, tmp_path) -> None:  # noqa: ANN001
    from app.agents.writer import WriterAgent
    from app.llm.fake_client import FakeScript
    from tests.engine_helpers import match, started_workflow
    from tests.test_budgets import fake_router
    from tests.tool_helpers import BASE_AGENTS, BASE_TOOLS, call, final, use_tools

    monkeypatch.setattr(WriterAgent, "allowed_tools", ("send_email",))
    use_tools(monkeypatch, tools={**BASE_TOOLS, "send_email": {**BASE_TOOLS["send_email"], "approval": "always",
                                                               "allowed_recipients": ["*@example.com", "*@outside.com"]}},
              agents={**BASE_AGENTS, "writer": ["send_email"]})
    use_policies(monkeypatch, {"name": "outside", "when": {"tool": "send_email",
                                                           "recipient_not_in_domains": ["example.com"]},
                               "approvals": 2, "approver_role": "approver"})
    report = {"title": "t", "content": "c", "word_count": 1}
    router, _ = fake_router(session_factory, llm=FakeLLMClient(scripts=[FakeScript(match=match("w"), steps=[
        call("send_email", to=["a@example.com"], subject="s", body="b"), final(report)])]))
    wf = await started_workflow(client, [task("w", "writer")])
    await make_engine(router, outbox_dir=tmp_path / "outbox").run_until_idle()
    a = (await client.get(f"/approvals?status=pending&workflow_id={wf}")).json()[0]
    assert a["required_approvals"] == 1  # inside the company: one approval
    r = await client.post(f"/approvals/{a['id']}/approve", json={
        "actor": "dana", "arguments_overrides": {"to": ["x@outside.com"]}})
    assert r.status_code == 200 and r.json()["status"] == "pending"  # the edit makes it need 2 people
    assert r.json()["required_approvals"] == 2 and r.json()["policy_rules"] == ["outside"]
