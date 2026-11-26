"""GET /tools: every registered tool, whether it is enabled, its approval mode and which agents may use it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config import get_settings
from app.tools.config import approval_mode, disabled_reason, get_tools_config
from app.tools.registry import all_tools

router = APIRouter(tags=["tools"])


@router.get("/tools")
async def list_tools() -> dict[str, Any]:
    cfg, settings = get_tools_config(), get_settings()
    out = []
    for name, tool in all_tools().items():
        reason = disabled_reason(tool, cfg, settings)
        entry = cfg.entry(name) if cfg else None
        out.append({
            "name": name,
            "description": tool.description,
            "side_effect": tool.side_effect,
            "untrusted": tool.untrusted,
            "input_schema": tool.input_model.model_json_schema(),
            "enabled": reason is None,
            "disabled_reason": reason,
            "approval": approval_mode(tool, cfg),
            "on_reject": entry.on_reject if entry else "inform_agent",
            "agents": sorted(a for a, names in (cfg.agents.items() if cfg else []) if name in names),
        })
    return {"configured": cfg is not None, "tools": out}
