from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient


def make_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Demo workflow",
        "goal": "Write a short report on electric vehicles",
        "tasks": [
            {
                "key": "research",
                "name": "Research EVs",
                "description": "Collect facts",
                "agent_type": "researcher",
                "input": {"topic": "EVs"},
                "requires_approval": False,
                "depends_on": [],
            },
            {
                "key": "write",
                "name": "Write report",
                "description": "Draft the report",
                "agent_type": "writer",
                "depends_on": ["research"],
            },
            {
                "key": "review",
                "name": "Review report",
                "description": "Check the report",
                "agent_type": "reviewer",
                "requires_approval": True,
                "depends_on": ["research", "write"],
            },
        ],
    }
    payload.update(overrides)
    return payload


async def create(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    resp = await client.post("/workflows", json=make_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- create / get


async def test_create_workflow_with_tasks_and_dependencies(client: AsyncClient) -> None:
    body = await create(client)

    assert body["name"] == "Demo workflow"
    assert body["status"] == "PENDING"
    assert body["context"] == {}
    assert body["started_at"] is None and body["completed_at"] is None

    tasks = {t["key"]: t for t in body["tasks"]}
    assert [t["key"] for t in body["tasks"]] == ["research", "write", "review"]  # request order
    assert tasks["research"]["depends_on"] == []
    assert tasks["write"]["depends_on"] == ["research"]
    assert tasks["review"]["depends_on"] == ["research", "write"]
    assert tasks["research"]["input"] == {"topic": "EVs"}
    assert tasks["review"]["requires_approval"] is True
    for t in tasks.values():
        assert t["status"] == "PENDING"
        assert t["attempt_count"] == 0
        assert t["max_attempts"] == 3
        assert t["workflow_id"] == body["id"]


async def test_create_workflow_without_tasks(client: AsyncClient) -> None:
    body = await create(client, tasks=[])
    assert body["tasks"] == []


async def test_get_workflow_by_id(client: AsyncClient) -> None:
    created = await create(client)

    resp = await client.get(f"/workflows/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert {t["key"]: t["depends_on"] for t in body["tasks"]} == {
        "research": [],
        "write": ["research"],
        "review": ["research", "write"],
    }


async def test_get_workflow_tasks(client: AsyncClient) -> None:
    created = await create(client)

    resp = await client.get(f"/workflows/{created['id']}/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert [t["key"] for t in tasks] == ["research", "write", "review"]
    assert tasks[1]["depends_on"] == ["research"]


async def test_unknown_workflow_returns_404(client: AsyncClient) -> None:
    missing = uuid.uuid4()
    for path in (f"/workflows/{missing}", f"/workflows/{missing}/tasks", f"/workflows/{missing}/events"):
        resp = await client.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "not_found"
        assert str(missing) in resp.json()["error"]["message"]


async def test_invalid_uuid_returns_422(client: AsyncClient) -> None:
    resp = await client.get("/workflows/not-a-uuid")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------- list


async def test_list_workflows_newest_first_with_pagination(client: AsyncClient) -> None:
    ids = [(await create(client, name=f"wf-{i}", tasks=[]))["id"] for i in range(3)]

    resp = await client.get("/workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [w["id"] for w in body["items"]] == list(reversed(ids))

    page = (await client.get("/workflows", params={"limit": 1, "offset": 1})).json()
    assert page["total"] == 3 and page["limit"] == 1 and page["offset"] == 1
    assert [w["id"] for w in page["items"]] == [ids[1]]


async def test_list_workflows_status_filter(client: AsyncClient) -> None:
    await create(client, tasks=[])

    pending = (await client.get("/workflows", params={"status": "PENDING"})).json()
    assert pending["total"] == 1
    running = (await client.get("/workflows", params={"status": "RUNNING"})).json()
    assert running["total"] == 0 and running["items"] == []


async def test_list_workflows_invalid_status_returns_422(client: AsyncClient) -> None:
    resp = await client.get("/workflows", params={"status": "BOGUS"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------- validation


def _task(key: str, depends_on: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "name": key, "description": f"Do {key}", "agent_type": "researcher",
            "depends_on": depends_on or []}


@pytest.mark.parametrize(
    ("tasks", "expected_fragment"),
    [
        ([_task("a"), _task("a")], "Duplicate task key 'a'"),
        ([_task("a", ["ghost"])], "depends on unknown task 'ghost'"),
        ([_task("a", ["a"])], "cannot depend on itself"),
        ([_task("a"), _task("b", ["a", "a"])], "more than once"),
    ],
    ids=["duplicate-keys", "unknown-dependency", "self-dependency", "duplicate-edge"],
)
async def test_invalid_task_graph_returns_422(
    client: AsyncClient, tasks: list[dict[str, Any]], expected_fragment: str
) -> None:
    resp = await client.post("/workflows", json=make_payload(tasks=tasks))
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert expected_fragment in error["message"]

    # Nothing may be persisted when validation fails.
    assert (await client.get("/workflows")).json()["total"] == 0


async def test_missing_required_fields_returns_422(client: AsyncClient) -> None:
    resp = await client.post("/workflows", json={"name": "no goal"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert "goal" in error["message"]


# ---------------------------------------------------------------- events


async def test_creation_events_are_recorded(client: AsyncClient) -> None:
    created = await create(client)

    resp = await client.get(f"/workflows/{created['id']}/events")
    assert resp.status_code == 200
    events = resp.json()

    assert [e["event_type"] for e in events] == [
        "workflow.created",
        "task.created",
        "task.created",
        "task.created",
    ]
    assert events[0]["task_id"] is None
    assert events[0]["payload"] == {"name": "Demo workflow", "task_count": 3}

    task_ids = {t["key"]: t["id"] for t in created["tasks"]}
    task_events = {e["payload"]["key"]: e for e in events[1:]}
    assert task_events["write"]["task_id"] == task_ids["write"]
    assert task_events["write"]["payload"]["depends_on"] == ["research"]
    assert task_events["review"]["payload"]["agent_type"] == "reviewer"

    # Chronological order (ids strictly increasing).
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
