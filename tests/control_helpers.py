"""Shared helpers for Part 6 (human controls) tests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def pending_approvals(client: AsyncClient, wf_id: str) -> list[dict[str, Any]]:
    resp = await client.get("/approvals", params={"status": "pending", "workflow_id": wf_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def only_pending(client: AsyncClient, wf_id: str) -> dict[str, Any]:
    rows = await pending_approvals(client, wf_id)
    assert len(rows) == 1, rows
    return rows[0]


async def events(client: AsyncClient, wf_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
    rows = (await client.get(f"/workflows/{wf_id}/events")).json()
    return [e for e in rows if event_type is None or e["event_type"] == event_type]


async def control(client: AsyncClient, wf_id: str, action: str, **body: Any) -> Any:
    return await client.post(f"/workflows/{wf_id}/{action}", json=body or None)
