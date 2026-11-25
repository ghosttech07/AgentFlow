from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import AgentInput, create_agent, publisher
from app.agents.writer import WritingOutput
from app.config import get_settings
from app.llm.demo_fake import build_demo_client
from app.llm.fake_client import FakeLLMClient
from app.models.approval import Publication
from app.services.planning_service import plan
from tests.control_helpers import only_pending
from tests.engine_helpers import detail, started_workflow, task, tasks_by_key

WRITING = {"title": "EV guide", "content": "# EV guide\n\nElectric cars are growing fast.", "word_count": 7}


async def count_rows(sf: async_sessionmaker[AsyncSession], key: str) -> int:
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(Publication)
                              .where(Publication.idempotency_key == key))


def files_for(wf_id: str) -> list[Path]:
    return sorted(get_settings().publish_dir.glob(f"{wf_id}__*.md"))


async def new_workflow(client: AsyncClient) -> str:
    return (await client.post("/workflows", json={"name": "p", "goal": "g"})).json()["id"]


def run_input(wf_id: str, **task_input: object) -> AgentInput:
    return AgentInput(workflow_goal="g", workflow_id=wf_id, task_key="pub", task_name="Publish",
                      task_input=task_input, dependency_outputs={"write": WRITING})


async def run_publisher(sf: async_sessionmaker[AsyncSession], agent_input: AgentInput) -> dict:
    agent = create_agent("publisher", FakeLLMClient())
    agent.session_factory = sf
    return (await agent.run(agent_input)).output


async def test_end_to_end_needs_approval_and_publishes_once(client: AsyncClient, make_engine,  # noqa: ANN001
                                                            session_factory) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("write", "writer"), task("pub", "publisher", ["write"])])
    llm = FakeLLMClient({WritingOutput: WRITING})
    engine = make_engine(llm)
    await engine.run_until_idle()
    wf = await detail(client, wf_id)
    assert wf["status"] == "AWAITING_APPROVAL"  # sensitive: forced requires_approval
    a = await only_pending(client, wf_id)
    assert "marked sensitive" in a["request_payload"]["why_sensitive"]
    assert "Electric cars" in a["request_payload"]["dependency_previews"]["write"]

    await client.post(f"/approvals/{a['id']}/approve", json={"actor": "ed", "input_overrides": {"title": "Final"}})
    await engine.run_until_idle()
    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    out = (await tasks_by_key(client, wf_id))["pub"]["output"]
    assert out["published"] is True and out["already_published"] is False
    assert out["idempotency_key"] == f"{wf_id}:pub"
    (path,) = files_for(wf_id)
    assert out["location"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert "title: Final" in text and "Electric cars are growing fast." in text
    assert await count_rows(session_factory, f"{wf_id}:pub") == 1
    assert len(llm.calls) == 1  # only the writer: the publisher never calls the LLM


async def test_second_run_returns_existing_publication(client: AsyncClient, session_factory) -> None:  # noqa: ANN001
    wf_id = await new_workflow(client)
    first = await run_publisher(session_factory, run_input(wf_id))
    second = await run_publisher(session_factory, run_input(wf_id, title="changed later"))
    assert (first["already_published"], second["already_published"]) == (False, True)
    assert second["location"] == first["location"] and second["published_at"] == first["published_at"]
    assert await count_rows(session_factory, f"{wf_id}:pub") == 1
    assert len(files_for(wf_id)) == 1
    assert "changed later" not in Path(first["location"]).read_text(encoding="utf-8")  # not re-published


async def test_crash_between_file_write_and_insert(client: AsyncClient, session_factory,  # noqa: ANN001
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    wf_id = await new_workflow(client)
    real_record = publisher._record

    async def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("process died after writing the file")

    monkeypatch.setattr(publisher, "_record", crash)
    with pytest.raises(RuntimeError):
        await run_publisher(session_factory, run_input(wf_id))
    (path,) = files_for(wf_id)  # the file exists, the row does not
    before = path.read_bytes()
    assert await count_rows(session_factory, f"{wf_id}:pub") == 0

    monkeypatch.setattr(publisher, "_record", real_record)
    out = await run_publisher(session_factory, run_input(wf_id))  # the retry
    assert out["already_published"] is False
    assert await count_rows(session_factory, f"{wf_id}:pub") == 1
    assert files_for(wf_id) == [path] and path.read_bytes() == before  # same file, same bytes
    assert not list(path.parent.glob(".*.tmp"))  # no temp files left behind


async def test_nothing_to_publish_is_permanent(client: AsyncClient, session_factory) -> None:  # noqa: ANN001
    wf_id = await new_workflow(client)
    agent_input = run_input(wf_id).model_copy(update={"dependency_outputs": {}})
    with pytest.raises(Exception) as exc:
        await run_publisher(session_factory, agent_input)
    assert getattr(exc.value, "error_type", None) == "permanent"
    resp = await client.post("/agents/publisher/run", json={"workflow_goal": "g", "task_name": "p"})
    assert resp.status_code == 502  # no workflow: can't be used to bypass approvals


async def test_fake_planner_adds_publisher_only_when_asked() -> None:
    with_publish = await plan(build_demo_client(0), "Write a guide on EVs and publish it")
    spec = with_publish.specs[-1]
    assert (spec.key, spec.agent_type, spec.requires_approval) == ("publish_report", "publisher", True)
    assert set(spec.depends_on) == {"write_report", "review_report"}
    without = await plan(build_demo_client(0), "Write a guide on EVs")
    assert "publisher" not in {s.agent_type for s in without.specs}
