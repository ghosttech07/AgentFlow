"""Part 12 Step 0: route permission declarations, login + sessions + CSRF + lockout, AUTH_MODE, headers, leftovers."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import update

from app.api.auth import get_principal
from app.config import Settings
from app.llm.fake_client import FakeLLMClient
from app.main import create_app
from app.models.governance import LoginAttempt
from tests.engine_helpers import task
from tests.test_governance import TWO, create, setup_org, use_policies

# Routes that are deliberately public (no permission). Everything else must resolve a principal.
PUBLIC = {"/health", "/health/live", "/health/ready", "/dashboard", "/auth/login", "/auth/logout"}


def _calls(dependant) -> set:  # noqa: ANN001
    out = {dependant.call}
    for d in dependant.dependencies:
        out |= _calls(d)
    return out


def test_every_route_declares_a_permission() -> None:
    app = create_app()
    missing = [f"{sorted(r.methods)} {r.path}" for r in app.routes if isinstance(r, APIRoute)
               and r.path not in PUBLIC and get_principal not in _calls(r.dependant)]
    assert missing == [], f"routes without a permission dependency: {missing}"


async def _with_password(client: AsyncClient, keys: dict, name: str = "pat", role: str = "operator") -> None:
    r = await client.post("/admin/users", json={"name": name, "role": role, "team": "research",
                                                "password": "correct horse battery"}, headers=keys["root"])
    assert r.status_code == 201, r.text


async def test_login_session_csrf_and_logout(client: AsyncClient) -> None:
    keys = await setup_org(client)
    await _with_password(client, keys)
    r = await client.post("/auth/login", json={"name": "pat", "password": "correct horse battery"})
    assert r.status_code == 200
    set_cookie = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith("af_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie.replace("Strict", "strict")
    csrf = r.json()["csrf_token"]
    assert (await client.get("/me")).json()["name"] == "pat"  # the cookie authenticates reads
    assert (await client.post("/workflows", json={"name": "w", "goal": "g"})).status_code == 403  # no CSRF header
    r = await client.post("/workflows", json={"name": "w", "goal": "g"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    await client.post("/auth/logout")
    client.cookies.clear()
    assert (await client.get("/me")).json()["kind"] == "anonymous"


async def test_no_user_enumeration_and_lockout_survives(client: AsyncClient, session_factory) -> None:  # noqa: ANN001
    keys = await setup_org(client)
    await _with_password(client, keys)
    wrong_pw = await client.post("/auth/login", json={"name": "pat", "password": "wrong password!!"})
    no_user = await client.post("/auth/login", json={"name": "nobody", "password": "wrong password!!"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["error"]["message"] == no_user.json()["error"]["message"]
    for _ in range(4):
        await client.post("/auth/login", json={"name": "pat", "password": "wrong password!!"})
    r = await client.post("/auth/login", json={"name": "pat", "password": "correct horse battery"})
    assert r.status_code == 429  # locked, even with the right password
    # the lock is in the DB (a restart doesn't clear it); expire it and the login works again
    async with session_factory() as s:
        await s.execute(update(LoginAttempt).values(locked_until=None, failures=0))
        await s.commit()
    assert (await client.post("/auth/login", json={"name": "pat", "password": "correct horse battery"})).status_code == 200


def test_auth_mode_off_is_refused_in_production() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=off"):
        Settings(_env_file=None, app_env="production", admin_api_key="k", auth_mode="off")
    assert Settings(_env_file=None, app_env="production", admin_api_key="k").auth_mode == "required"


async def test_auth_mode_required_closes_open_mode(client: AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_mode", "required")
    assert (await client.get("/workflows")).status_code == 401  # no users yet, but no open mode either


async def test_security_headers(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers  # no CORS by default


def test_policy_errors_fail_closed() -> None:
    from app.governance.policy import Policies, Rule

    pols = Policies([Rule.model_validate({"name": "r", "when": {"tool": "send_email"}, "approvals": 1}),
                     Rule.model_validate({"name": "t", "when": {"agent_type": "writer"}, "approvals": 1})])
    pols._match = lambda r, ctx: 1 / 0  # a bug in evaluation
    tool = pols.evaluate(scope="tool", tool="send_email")
    task_req = pols.evaluate(scope="task", agent_type="writer")
    assert tool.deny and tool.rules == ["policy_error"]
    assert task_req.require and not task_req.deny


# ------------------------------------------------------------------------------ leftovers
async def test_team_admin_lists_only_their_team(client: AsyncClient) -> None:
    keys = await setup_org(client)
    r = await client.post("/admin/users", json={"name": "lead", "role": "admin", "team": "research"},
                          headers=keys["root"])
    lead = {"X-API-Key": r.json()["api_key"]}
    assert [t["name"] for t in (await client.get("/admin/teams", headers=lead)).json()] == ["research"]
    assert len((await client.get("/admin/teams", headers=keys["root"])).json()) == 2  # superadmin sees all
    # unknown team vs another team's: the same answer (no team-name enumeration)
    a = await client.post("/admin/users", json={"name": "z1", "role": "viewer", "team": "marketing"}, headers=lead)
    b = await client.post("/admin/users", json={"name": "z2", "role": "viewer", "team": "nope"}, headers=lead)
    assert a.status_code == b.status_code == 403


async def test_memory_purge_is_team_scoped(client: AsyncClient) -> None:
    keys = await setup_org(client)
    r = await client.post("/admin/users", json={"name": "lead", "role": "admin", "team": "research"},
                          headers=keys["root"])
    lead = {"X-API-Key": r.json()["api_key"]}
    assert (await client.post("/memories/purge-expired", headers=lead)).json()["scope"] == "your team"
    assert (await client.post("/memories/purge-expired", headers=keys["root"])).json()["scope"] == "all teams"


async def test_unsatisfiable_approval_is_flagged_and_escalated(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    use_policies(monkeypatch, {**TWO, "approvals": 3})  # research has ann + bob (+ root, global) = 3 approvers
    keys = await setup_org(client)
    wf = await create(client, keys["ann"], [task("p", "publisher")])  # ann created it: four eyes leaves 2
    await make_engine(FakeLLMClient()).run_until_idle()
    a = (await client.get(f"/approvals?workflow_id={wf}", headers=keys["olga"])).json()[0]
    assert a["request_payload"]["unsatisfiable"] == "needs 3 approvers, only 2 eligible"
    assert "root" in a["request_payload"]["escalated_to"]
    types = [e["event_type"] for e in (await client.get(f"/workflows/{wf}/events", headers=keys["olga"])).json()]
    assert "approval.unsatisfiable" in types and "approval.escalated" in types
    w = (await client.get("/admin/policies", headers=keys["root"])).json()["warnings"]
    assert any(x["team"] == "research" and x["needs"] == 3 for x in w)
