from __future__ import annotations

import asyncio
import uuid

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.researcher import ResearchOutput
from app.core.enums import TaskStatus
from app.engine.scheduler import claim_tasks
from app.llm.errors import LLMTransientError
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.models.approval import Approval
from app.models.task import Task
from app.services import notify
from tests.control_helpers import events, only_pending, pending_approvals
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key
from tests.reliability_helpers import attempts, calls_for, outcomes

GATED = [task("research"), task("write", "writer", ["research"], requires_approval=True, input={"audience": "kids"})]


async def approve(client: AsyncClient, approval_id: str, **body: object):  # noqa: ANN201
    return await client.post(f"/approvals/{approval_id}/approve", json={"actor": "bob", **body})


async def reject(client: AsyncClient, approval_id: str, **body: object):  # noqa: ANN201
    return await client.post(f"/approvals/{approval_id}/reject", json={"actor": "bob", "reason": "no", **body})


async def test_request_payload_has_everything_to_decide(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    big = {"summary": "x" * 5000, "key_findings": ["f1", "f2", "f3"], "sources": [], "confidence": "high",
           "gaps": []}
    wf_id = await started_workflow(client, GATED, goal="Explain EVs")
    await make_engine(FakeLLMClient({ResearchOutput: big})).run_until_idle()

    wf = await detail(client, wf_id)
    a = await only_pending(client, wf_id)
    assert wf["pending_approvals"] == {"count": 1, "ids": [a["id"]]}
    p = a["request_payload"]
    assert p["workflow_goal"] == "Explain EVs" and p["task_key"] == "write" and p["task_name"] == "Task write"
    assert p["task_description"] == "Do write." and p["agent_type"] == "writer"
    assert "requires_approval" in p["why_sensitive"] and p["task_input"] == {"audience": "kids"}
    preview = p["dependency_previews"]["research"]
    assert '"summary": "xxx' in preview and "truncated, 5" in preview and len(preview) < 1600
    assert a["expires_at"] is not None  # default 24 h
    assert (await events(client, wf_id, "approval.requested"))[0]["payload"]["approval_id"] == a["id"]


async def test_approve_with_overrides_runs_and_completes(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    llm = FakeLLMClient()
    engine = make_engine(llm)
    await engine.run_until_idle()
    a = await only_pending(client, wf_id)

    resp = await approve(client, a["id"], comment="ok", input_overrides={"audience": "adults", "tone": "calm"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved" and resp.json()["decided_by"] == "bob"
    wf = await detail(client, wf_id)
    assert wf["status"] == "RUNNING"
    assert {t["key"]: t for t in wf["tasks"]}["write"]["status"] == "READY"
    change = (await events(client, wf_id, "approval.approved"))[0]["payload"]
    assert change["input_changes"] == {"audience": {"from": "kids", "to": "adults"},
                                       "tone": {"from": None, "to": "calm"}}

    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    prompt = next(c["user_prompt"] for c in llm.calls if "Task write" in c["user_prompt"])
    assert '"audience": "adults"' in prompt and '"tone": "calm"' in prompt
    # Double approve / late reject -> 409
    assert (await approve(client, a["id"])).status_code == 409
    assert (await reject(client, a["id"])).status_code == 409


async def test_concurrent_approvals_exactly_one_wins(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    await make_engine(FakeLLMClient()).run_until_idle()
    a = await only_pending(client, wf_id)
    r1, r2, r3 = await asyncio.gather(approve(client, a["id"]), approve(client, a["id"]), reject(client, a["id"]))
    assert sorted([r1.status_code, r2.status_code, r3.status_code]) == [200, 409, 409]
    decided = [e for e in await events(client, wf_id) if e["event_type"] in ("approval.approved", "approval.rejected")]
    assert len(decided) == 1


async def test_reject_fail_workflow(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    graph = [*GATED, task("after", "reviewer", ["write"])]
    wf_id = await started_workflow(client, graph)
    llm = FakeLLMClient()
    await make_engine(llm).run_until_idle()
    a = await only_pending(client, wf_id)
    assert (await reject(client, a["id"], reason="wrong audience")).status_code == 200

    wf = await detail(client, wf_id)
    assert wf["status"] == "FAILED" and "rejected" in wf["error"]
    tasks = {t["key"]: t for t in wf["tasks"]}
    assert tasks["write"]["status"] == "FAILED" and tasks["write"]["error_type"] == "rejected"
    assert "wrong audience" in tasks["write"]["error"]
    assert tasks["after"]["status"] == "PENDING"
    assert calls_for(llm, "write") == 0


async def test_reject_skip_task_cascades_and_completes(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    graph = [task("research"), task("gate", "writer", ["research"], requires_approval=True),
             task("w", "writer", ["gate"]), task("v", "reviewer", ["w", "research"]), task("side", "analyst", ["research"])]
    wf_id = await started_workflow(client, graph)
    llm = FakeLLMClient()
    engine = make_engine(llm)
    await engine.run_until_idle()
    a = await only_pending(client, wf_id)
    assert (await reject(client, a["id"], on_reject="skip_task")).status_code == 200
    await engine.run_until_idle()

    wf = await detail(client, wf_id)
    assert wf["status"] == "COMPLETED"
    tasks = {t["key"]: t["status"] for t in wf["tasks"]}
    assert tasks == {"research": "SUCCEEDED", "gate": "SKIPPED", "w": "SKIPPED", "v": "SKIPPED", "side": "SUCCEEDED"}
    assert sorted(wf["result"]["skipped"]) == ["gate", "v", "w"]
    skipped = await events(client, wf_id, "task.skipped")
    assert {e["payload"]["task_key"] for e in skipped} == {"w", "v"}
    assert all(e["payload"]["reason"] == "upstream_rejected" for e in skipped)
    assert calls_for(llm, "gate") == calls_for(llm, "w") == calls_for(llm, "v") == 0


async def test_expired_approval_fails_workflow(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    engine = make_engine(FakeLLMClient(), approval_timeout_hours=0.3 / 3600)  # 0.3 s
    await engine.run_until_idle()
    a = await only_pending(client, wf_id)
    await asyncio.sleep(0.4)
    assert (await approve(client, a["id"])).status_code == 409  # too late, even before the tick

    await engine.run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "FAILED"
    t = {x["key"]: x for x in wf["tasks"]}["write"]
    assert t["status"] == "FAILED" and t["error_type"] == "approval_expired"
    assert (await client.get(f"/approvals/{a['id']}")).json()["status"] == "expired"
    assert await events(client, wf_id, "approval.expired")


async def test_never_expires_with_zero_timeout(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    await make_engine(FakeLLMClient(), approval_timeout_hours=0).run_until_idle()
    assert (await only_pending(client, wf_id))["expires_at"] is None


async def test_claim_refuses_unapproved_gated_task_even_if_ready(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    wf_id = await started_workflow(client, [task("g", requires_approval=True)])
    async with session_factory() as s:  # bypass the scheduler: force the status
        await s.execute(update(Task).where(Task.workflow_id == uuid.UUID(wf_id)).values(status=TaskStatus.READY))
        await s.commit()
        assert await claim_tasks(s, "w", limit=5, lease_seconds=60) == []
        task_id = (await tasks_by_key(client, wf_id))["g"]["id"]
        s.add(Approval(workflow_id=uuid.UUID(wf_id), task_id=uuid.UUID(task_id), status="approved"))
        await s.commit()
        assert len(await claim_tasks(s, "w", limit=5, lease_seconds=60)) == 1


async def test_approval_survives_automatic_retry(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("g", requires_approval=True)])
    llm = FakeLLMClient(scripts=[FakeScript(match("g"), error=LLMTransientError("503"), times=1)])
    engine = make_engine(llm)
    await engine.run_until_idle()
    await approve(client, (await only_pending(client, wf_id))["id"])
    await engine.run_until_idle()

    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    assert outcomes(await attempts(client, wf_id, "g")) == [(1, "failed", "transient"), (2, "succeeded", None)]
    rows = (await client.get("/approvals", params={"workflow_id": wf_id})).json()
    assert [r["status"] for r in rows] == ["approved"]  # no second approval for the retry


async def test_manual_retry_of_rejected_task_needs_new_approval(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    engine = make_engine(FakeLLMClient())
    await engine.run_until_idle()
    await reject(client, (await only_pending(client, wf_id))["id"])
    assert (await client.post(f"/workflows/{wf_id}/retry")).status_code == 202
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "AWAITING_APPROVAL"
    assert len(await pending_approvals(client, wf_id)) == 1


async def test_independent_branches_run_and_status_switches(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    graph = [task("gated", requires_approval=True), task("slow"), task("after", "analyst", ["slow"])]
    wf_id = await started_workflow(client, graph)
    llm = FakeLLMClient(scripts=[FakeScript(match("slow"), delay=0.3)])
    engine = make_engine(llm)
    await engine.run_until_idle()

    tasks = await tasks_by_key(client, wf_id)
    assert (tasks["gated"]["status"], tasks["slow"]["status"], tasks["after"]["status"]) == \
        ("AWAITING_APPROVAL", "SUCCEEDED", "SUCCEEDED")
    rows = await events(client, wf_id)
    kinds = [(e["event_type"], e["payload"].get("task_key")) for e in rows]
    # The workflow became AWAITING_APPROVAL only after the last runnable task finished.
    assert kinds.index(("workflow.awaiting_approval", None)) > kinds.index(("task.succeeded", "after"))
    assert kinds.index(("approval.requested", "gated")) < kinds.index(("task.succeeded", "slow"))

    await approve(client, (await only_pending(client, wf_id))["id"])
    assert (await detail(client, wf_id))["status"] == "RUNNING"
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"


async def test_approve_while_paused_keeps_paused(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    engine = make_engine(FakeLLMClient())
    await engine.run_until_idle()
    await client.post(f"/workflows/{wf_id}/pause")
    await approve(client, (await only_pending(client, wf_id))["id"])
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "PAUSED"
    assert (await tasks_by_key(client, wf_id))["write"]["status"] == "READY"
    assert (await client.post(f"/workflows/{wf_id}/resume")).json()["status"] == "RUNNING"
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"


async def test_resume_goes_to_awaiting_approval_when_only_approvals_block(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, GATED)
    await make_engine(FakeLLMClient()).run_until_idle()
    await client.post(f"/workflows/{wf_id}/pause")
    assert (await client.post(f"/workflows/{wf_id}/resume")).json()["status"] == "AWAITING_APPROVAL"


async def test_webhook_called_and_failure_is_harmless(client: AsyncClient, make_engine, monkeypatch) -> None:  # noqa: ANN001
    sent: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict) -> None:
        sent.append((url, payload))
        if len(sent) == 2:
            raise ConnectionError("hook down")

    monkeypatch.setattr(notify, "_post", fake_post)
    for _ in range(2):
        wf_id = await started_workflow(client, GATED)
        await make_engine(FakeLLMClient(), approval_webhook_url="http://hook.test/approvals").run_until_idle()
        await asyncio.gather(*notify.pending)
        assert (await detail(client, wf_id))["status"] == "AWAITING_APPROVAL"  # 2nd: hook failed, no harm
    url, payload = sent[0]
    assert url == "http://hook.test/approvals"
    assert payload["task_key"] == "write" and payload["approval_id"]
