"""Shared helpers for engine tests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


def task(key: str, agent: str = "researcher", deps: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """Manual task spec. The name 'Task <key>' lets FakeScript target it via '## YOUR TASK\\nTask <key>'."""
    return {"key": key, "name": f"Task {key}", "description": f"Do {key}.", "agent_type": agent,
            "depends_on": deps or [], **extra}


def match(key: str) -> str:
    """FakeScript.match string that only hits the prompt of task `key`."""
    return f"## YOUR TASK\nTask {key}\n"


async def started_workflow(client: AsyncClient, tasks: list[dict[str, Any]], goal: str = "Test goal") -> str:
    resp = await client.post("/workflows", json={"name": "wf", "goal": goal, "tasks": tasks})
    assert resp.status_code == 201, resp.text
    wf_id = resp.json()["id"]
    start = await client.post(f"/workflows/{wf_id}/start")
    assert start.status_code == 202, start.text
    return wf_id


async def detail(client: AsyncClient, wf_id: str) -> dict[str, Any]:
    return (await client.get(f"/workflows/{wf_id}")).json()


async def tasks_by_key(client: AsyncClient, wf_id: str) -> dict[str, dict[str, Any]]:
    return {t["key"]: t for t in (await detail(client, wf_id))["tasks"]}


async def event_types(client: AsyncClient, wf_id: str) -> list[str]:
    return [e["event_type"] for e in (await client.get(f"/workflows/{wf_id}/events")).json()]
