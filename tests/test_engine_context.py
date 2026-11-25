from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.writer import WritingOutput
from app.engine.context import build_agent_input
from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.engine_helpers import match, started_workflow, task, tasks_by_key


def research(marker: str) -> dict:
    return {"summary": marker, "key_findings": [f"{marker}-1", f"{marker}-2", f"{marker}-3"],
            "sources": [], "confidence": "high", "gaps": []}


async def test_dependents_receive_direct_dependency_outputs(
    client: AsyncClient, make_engine, db_session: AsyncSession  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(
        client,
        [task("a"), task("b", deps=["a"]), task("c", deps=["a"]), task("d", "writer", ["b", "c"])],
        goal="Explain heat pumps",
    )
    llm = FakeLLMClient(scripts=[
        FakeScript(match("a"), response=research("A-SECRET")),
        FakeScript(match("b"), response=research("B-SECRET")),
        FakeScript(match("c"), response=research("C-SECRET")),
    ], responses={WritingOutput: {"title": "t", "content": "words here", "word_count": 2}})
    await make_engine(llm).run_until_idle()

    d_prompt = next(c["user_prompt"] for c in llm.calls if "## YOUR TASK\nTask d" in c["user_prompt"])
    assert "Explain heat pumps" in d_prompt
    assert "### Task 'b'" in d_prompt and "B-SECRET" in d_prompt
    assert "### Task 'c'" in d_prompt and "C-SECRET" in d_prompt
    assert "A-SECRET" not in d_prompt  # only DIRECT dependencies are passed

    # Same thing read straight from the DB.
    d_id = uuid.UUID((await tasks_by_key(client, wf_id))["d"]["id"])
    _, agent_input = await build_agent_input(db_session, d_id)
    assert set(agent_input.dependency_outputs) == {"b", "c"}
    assert agent_input.dependency_outputs["b"]["summary"] == "B-SECRET"
    assert agent_input.task_key == "d" and agent_input.workflow_goal == "Explain heat pumps"
