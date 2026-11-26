"""Startup: load + validate tools.yaml (raises ToolConfigError) and log which tools are usable."""

from __future__ import annotations

import logging

from app.config import get_settings
from app.tools.config import disabled_reason, get_tools_config
from app.tools.registry import all_tools

logger = logging.getLogger(__name__)


def log_tools_config() -> None:
    import app.tools.catalog  # noqa: F401  (registers every tool)

    get_tools_config.cache_clear()
    cfg = get_tools_config()
    if cfg is None:
        logger.info("tools.not_configured (no tools.yaml): agents run without tools")
        return
    settings = get_settings()
    for name, tool in all_tools().items():
        reason = disabled_reason(tool, cfg, settings)
        if reason is None:
            logger.info("tools.enabled tool=%s side_effect=%s", name, tool.side_effect)
        elif cfg.entry(name) is not None:
            logger.info("tools.disabled tool=%s reason=%s", name, reason)
