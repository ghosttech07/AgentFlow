from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.engine import chaos
from app.llm.fake_client import FakeLLMClient
from app.models import ChaosRule
from tests.engine_helpers import detail, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, outcomes


async def add_rule(client: AsyncClient, wf_id: str, key: str, mode: str, times: int = 1) -> dict:
    resp = await client.post("/dev/chaos", json={"workflow_id": wf_id, "task_key": key, "mode": mode,
                                                 "times": times})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("mode", "first_error_type", "final_status"),
    [("raise_transient", "transient", "SUCCEEDED"),
     ("raise_overloaded", "overloaded", "SUCCEEDED"),
     ("raise_permanent", "permanent", "FAILED"),
     ("hang", "timeout", "SUCCEEDED")],
)
async def test_error_modes(client: AsyncClient, make_engine, mode: str, first_error_type: str,  # noqa: ANN001
                           final_status: str) -> None:
    wf_id = await started_workflow(client, [task("a")])
    await add_rule(client, wf_id, "a", mode)
    # Only "hang" needs a short task timeout; for the other modes the timed region includes the chaos rule's DB
    # round-trips, which can exceed 0.3 s on a slow runner and turn the injected error into a timeout (seen on CI).
    timeout = 0.3 if mode == "hang" else 10
    await make_engine(FakeLLMClient(), task_timeout_seconds=timeout).run_until_idle()

    t = (await tasks_by_key(client, wf_id))["a"]
    assert t["status"] == final_status
    rows = outcomes(await attempts(client, wf_id, "a"))
    assert rows[0] == (1, "failed", first_error_type)
    if final_status == "SUCCEEDED":
        assert rows[1] == (2, "succeeded", None)  # rule used up: the retry runs normally
    rules = (await client.get("/dev/chaos", params={"workflow_id": wf_id})).json()
    assert rules[0]["remaining"] == 0


async def test_crash_process_commits_before_exit(
    client: AsyncClient, make_engine, session_factory: async_sessionmaker[AsyncSession],  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    await add_rule(client, wf_id, "a", "crash_process")
    exits: list[int] = []

    def fake_exit(code: int) -> None:
        exits.append(code)
        raise RuntimeError("simulated hard exit")  # stops the worker exactly where os._exit would

    monkeypatch.setattr(chaos, "_hard_exit", fake_exit)
    await make_engine(FakeLLMClient()).run_until_idle()

    assert exits == [1]
    async with session_factory() as s:  # decrement was committed before the "exit"
        rule = await s.scalar(select(ChaosRule).where(ChaosRule.workflow_id == uuid.UUID(wf_id)))
        assert rule.remaining == 0
    assert (await detail(client, wf_id))["status"] == "COMPLETED"  # no second crash


async def test_chaos_api_and_production_guard(client: AsyncClient, app) -> None:  # noqa: ANN001
    wf_id = (await client.post("/workflows", json={"name": "x", "goal": "g"})).json()["id"]
    rule = await add_rule(client, wf_id, "a", "hang", times=2)
    assert rule["remaining"] == 2
    assert len((await client.get("/dev/chaos")).json()) == 1
    assert (await client.delete(f"/dev/chaos/{rule['id']}")).status_code == 204
    assert (await client.get("/dev/chaos")).json() == []
    bad = await client.post("/dev/chaos", json={"workflow_id": wf_id, "task_key": "a", "mode": "explode"})
    assert bad.status_code == 422

    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update={"app_env": "production"})
    assert (await client.get("/dev/chaos")).status_code == 404


async def test_chaos_is_inert_in_production(session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings = get_settings().model_copy(update={"app_env": "production"})
    await chaos.inject(session_factory, uuid.uuid4(), "a", settings)  # returns without touching the DB


async def test_crash_after_tool_fires_only_after_that_tool_succeeded(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf_id = await started_workflow(client, [task("a")])
    await add_rule(client, wf_id, "a", "crash_after_tool:send_email")
    exits: list[int] = []
    monkeypatch.setattr(chaos, "_hard_exit", lambda code: exits.append(code))
    settings = get_settings()
    await chaos.inject(session_factory, uuid.UUID(wf_id), "a", settings)  # agent start: not this rule
    await chaos.after_tool(session_factory, uuid.UUID(wf_id), "a", "web_search", settings)  # other tool
    assert exits == []
    await chaos.after_tool(session_factory, uuid.UUID(wf_id), "a", "send_email", settings)
    await chaos.after_tool(session_factory, uuid.UUID(wf_id), "a", "send_email", settings)  # used up
    assert exits == [1]
