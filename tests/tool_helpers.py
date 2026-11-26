"""Shared helpers for Part 9 tool-loop tests: a tools config, step builders, journal reads."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ToolCall
from app.tools import config as tools_config
from app.tools.config import ToolsConfig

BASE_TOOLS: dict[str, Any] = {
    "web_search": {"backends": ["fake"]},
    "web_fetch": {"backend": "fake"},
    "calculator": {},
    "sql_query": {"sources": {"sample": {"url_env": "SAMPLE_DATA_URL"}}},
    "file_write": {},
    "send_email": {"backend": "outbox", "allowed_recipients": ["*@example.com"]},
    "webhook_post": {"allowed_urls": ["https://hooks.slack.com/services/"]},
}
BASE_AGENTS = {"researcher": ["web_search", "web_fetch"], "analyst": ["sql_query", "calculator"],
               "writer": ["file_write"], "communicator": ["send_email", "webhook_post"]}


def use_tools(monkeypatch: pytest.MonkeyPatch, tools: dict[str, Any] | None = None,
              agents: dict[str, list[str]] | None = None) -> ToolsConfig:
    import app.tools.catalog  # noqa: F401  (register every tool)

    cfg = ToolsConfig.model_validate({"tools": tools or BASE_TOOLS, "agents": agents or BASE_AGENTS})
    monkeypatch.setattr(tools_config, "get_tools_config", lambda: cfg)
    return cfg


def call(tool: str, reason: str = "needed", **arguments: Any) -> dict[str, Any]:
    return {"action": "tool_call", "tool": tool, "arguments": arguments, "reason": reason, "output": None}


def final(output: dict[str, Any]) -> dict[str, Any]:
    return {"action": "final", "tool": None, "arguments": None, "reason": None, "output": output}


RESEARCH = {"summary": "EV sales rose [1].", "key_findings": ["a [1]", "b [1]", "c [1]"],
            "sources": [{"title": "India EV", "url": "https://news.example.com/india-ev-sales-2026"}],
            "confidence": "high", "gaps": []}


async def tool_rows(sf: async_sessionmaker[AsyncSession], wf_id: str | uuid.UUID) -> list[ToolCall]:
    async with sf() as s:
        return list(await s.scalars(select(ToolCall).where(ToolCall.workflow_id == uuid.UUID(str(wf_id)))
                                    .order_by(ToolCall.created_at)))
