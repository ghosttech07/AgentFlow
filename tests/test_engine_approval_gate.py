from __future__ import annotations

from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import detail, event_types, started_workflow, task, tasks_by_key


async def test_approval_gate_stops_before_running(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [
        task("research"),
        task("publish", "writer", ["research"], requires_approval=True),
    ])
    llm = FakeLLMClient()
    await make_engine(llm).run_until_idle()

    wf = await detail(client, wf_id)
    assert wf["status"] == "AWAITING_APPROVAL"
    tasks = await tasks_by_key(client, wf_id)
    assert tasks["research"]["status"] == "SUCCEEDED"
    assert tasks["publish"]["status"] == "AWAITING_APPROVAL"
    assert tasks["publish"]["attempt_count"] == 0
    assert len(llm.calls) == 1 and "Task publish" not in llm.calls[0]["user_prompt"]  # agent never called

    types = await event_types(client, wf_id)
    assert types[-2:] == ["approval.requested", "workflow.awaiting_approval"]


async def test_gated_root_task_never_calls_agent(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("only", requires_approval=True)])
    llm = FakeLLMClient()
    await make_engine(llm).run_until_idle()
    assert llm.calls == []
    assert (await detail(client, wf_id))["status"] == "AWAITING_APPROVAL"


async def test_gate_no_longer_blocks_independent_tasks(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    # Part 6: a pending approval only blocks its own branch (Part 4 froze the whole workflow).
    wf_id = await started_workflow(client, [task("gated", requires_approval=True), task("free")])
    llm = FakeLLMClient()
    await make_engine(llm).run_until_idle()
    tasks = await tasks_by_key(client, wf_id)
    assert tasks["gated"]["status"] == "AWAITING_APPROVAL"
    assert tasks["free"]["status"] == "SUCCEEDED"
    assert len(llm.calls) == 1 and "Task free" in llm.calls[0]["user_prompt"]
    assert (await detail(client, wf_id))["status"] == "AWAITING_APPROVAL"  # nothing else can run
