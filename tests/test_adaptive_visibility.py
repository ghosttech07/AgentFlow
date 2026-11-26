"""Part 10 visibility: timeline summary + readable lines, graph fields, dashboard sections."""

from __future__ import annotations

from httpx import AsyncClient

from app.agents.reviewer import ReviewOutput
from app.agents.writer import WritingOutput
from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import task
from tests.test_loops import LOOP, draft, review


async def test_timeline_and_graph_show_loops_and_branches(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    tasks = [task("research"), task("write_guide", "writer", ["research"]), task("review_guide", "reviewer", ["write_guide"]),
             task("deep", "researcher", ["research"], run_if="research.output.confidence == 'never'")]
    wf = (await client.post("/workflows", json={"name": "w", "goal": "g", "tasks": tasks, "loops": [LOOP]})).json()["id"]
    await client.post(f"/workflows/{wf}/start")
    await make_engine(FakeLLMClient({ReviewOutput: [review(5), review(9)], WritingOutput: [draft(1), draft(2)]})
                      ).run_until_idle()
    tl = (await client.get(f"/workflows/{wf}/timeline")).json()
    s = tl["summary"]
    assert (s["loop_iterations"], s["condition_skips"], s["replans"], s["failed_branches"]) == (1, 1, 0, 0)
    spans = {x["task_key"]: x for x in tl["spans"]}
    assert spans["write_guide__v2"]["iteration"] == 2 and spans["write_guide__v2"]["base_key"] == "write_guide"
    assert spans["deep"]["skip_reason"] == "condition_false"
    texts = " | ".join(e["text"] for e in tl["entries"])
    assert "Loop improve_draft: iteration v2 added" in texts and "Loop improve_draft done at v2" in texts
    assert "deep: run_if" in texts
    graph = (await client.get(f"/workflows/{wf}/graph")).json()
    node = next(n for n in graph["nodes"] if n["key"] == "deep")
    assert node["run_if"] and node["skip_reason"] == "condition_false"


async def test_dashboard_has_the_adaptive_views(client: AsyncClient) -> None:
    page = (await client.get("/dashboard")).text
    for needle in ("/plan-versions", "Plan history", "loop_exhausted", "plan_patch", "failed_branches", "/memories",
                   "showMemory", "/replan", "Re-planning"):
        assert needle in page, needle
