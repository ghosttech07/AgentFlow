from __future__ import annotations

from httpx import AsyncClient

from app.llm.errors import LLMTransientError
from app.llm.fake_client import FakeLLMClient, FakeScript
from app.services.timeline_service import describe
from tests.control_helpers import only_pending
from tests.engine_helpers import match, started_workflow, task


async def test_timeline_tells_the_whole_story(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [
        task("a"), task("b", deps=["a"]), task("pub", "writer", ["b"], requires_approval=True),
    ])
    llm = FakeLLMClient(scripts=[FakeScript(match("b"), error=LLMTransientError("503"), times=1)])
    engine = make_engine(llm)
    await engine.run_until_idle()
    a = await only_pending(client, wf_id)
    await client.post(f"/approvals/{a['id']}/approve", json={"actor": "ann", "input_overrides": {"x": 1}})
    await engine.run_until_idle()

    resp = await client.get(f"/workflows/{wf_id}/timeline")
    assert resp.status_code == 200, resp.text
    tl = resp.json()
    assert tl["status"] == "COMPLETED" and tl["duration_seconds"] is not None
    s = tl["summary"]
    assert (s["tasks"], s["attempts"], s["retries_scheduled"]) == (3, 4, 1)
    assert s["attempt_outcomes"] == {"succeeded": 3, "failed": 1}
    assert s["approvals"] == {"approved": 1} and s["models"] == ["fake-model"]

    spans = {sp["task_key"]: sp for sp in tl["spans"]}
    assert [x["outcome"] for x in spans["b"]["attempts"]] == ["failed", "succeeded"]
    assert spans["b"]["depends_on"] == ["a"]
    first, second = spans["b"]["attempts"]
    assert first["error_type"] == "transient" and first["duration_s"] >= 0
    assert first["start_offset_s"] <= second["start_offset_s"]

    offsets = [e["offset_s"] for e in tl["entries"]]
    assert offsets == sorted(offsets) and offsets[0] >= 0
    texts = [e["text"] for e in tl["entries"]]
    assert any(t.startswith("b attempt 1 failed (transient); retry in") for t in texts)
    assert "pub approved by ann, input changed: ['x']" in texts
    assert texts[-1].startswith("Workflow completed in")
    assert (await client.get("/workflows/00000000-0000-0000-0000-000000000000/timeline")).status_code == 404


def test_describe_covers_control_events_and_falls_back() -> None:
    assert describe("workflow.paused", {"mode": "immediate", "actor": "ops", "reason": "deploy"}) == \
        "Paused (immediate) by ops: deploy"
    assert describe("task.skipped", {"task_key": "w", "upstream": "gate"}) == "w skipped (upstream gate rejected)"
    assert describe("engine.recovered", {"worker_id": "w2", "reclaimed_task_keys": ["write"]}) == \
        "Engine restarted (w2); reclaimed ['write']"
    assert describe("task.status_changed", {"task_key": "a", "from": "RUNNING", "to": "READY"}) == "a RUNNING -> READY"
    assert describe("workflow.status_changed", {"from": "PENDING", "to": "PLANNING", "reason": "go"}) == \
        "Workflow PENDING -> PLANNING: go"


async def test_dashboard_is_served(client: AsyncClient) -> None:
    resp = await client.get("/dashboard")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    page = resp.text
    for needle in ("<title>AgentFlow Dashboard</title>", "/timeline", "/approvals/", "X-API-Key", "const esc"):
        assert needle in page
    assert "/dashboard" not in (await client.get("/openapi.json")).text  # not part of the API schema
