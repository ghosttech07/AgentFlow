"""Shared helpers for Part 5 tests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def attempts(client: AsyncClient, wf_id: str, key: str) -> list[dict[str, Any]]:
    resp = await client.get(f"/workflows/{wf_id}/tasks/{key}/attempts")
    assert resp.status_code == 200, resp.text
    return resp.json()


def outcomes(rows: list[dict[str, Any]]) -> list[tuple[int, str, str | None]]:
    return [(r["attempt_number"], r["outcome"], r["error_type"]) for r in rows]


def calls_for(llm: Any, key: str) -> int:
    """How many times the agent of task `key` was called (engine_helpers task naming)."""
    return sum(1 for c in llm.calls if f"## YOUR TASK\nTask {key}\n" in c["user_prompt"])
