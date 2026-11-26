"""config/tools.yaml: which tools are enabled, their settings, which agents may use them, credentials.

Validated at startup; a bad file stops the app (like models.yaml). No file = no agent has tools, so every
agent behaves exactly as before Part 9.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings, get_settings
from app.tools.base import Tool, ToolError

logger = logging.getLogger(__name__)
ApprovalMode = Literal["never", "always", "external_write_only"]


class ToolConfigError(ValueError):
    """Invalid tools.yaml: the app must not start."""


class ToolEntry(BaseModel):
    model_config = ConfigDict(extra="allow")  # tool-specific settings (allowlists, backends, limits)
    enabled: bool = True
    approval: ApprovalMode | None = None
    on_reject: Literal["inform_agent", "fail_task"] = "inform_agent"

    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class CredentialSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    env: str = Field(min_length=1)  # the env var holding the secret (the secret never lives in the file)
    header: str = "Authorization"
    format: str = "Bearer {secret}"


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: dict[str, ToolEntry] = Field(default_factory=dict)
    agents: dict[str, list[str]] = Field(default_factory=dict)
    credentials: dict[str, CredentialSpec] = Field(default_factory=dict)

    def entry(self, name: str) -> ToolEntry | None:
        return self.tools.get(name)


def _fail(path: object, message: str) -> ToolConfigError:
    return ToolConfigError(f"tools.yaml ({path}): {message}")


def load_tools_config(settings: Settings) -> ToolsConfig | None:
    from app.agents.registry import agent_types
    from app.tools.registry import all_tools

    path = settings.tools_config_path
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise _fail(path, f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise _fail(path, "the top level must be a mapping with tools / agents / credentials")
    try:
        cfg = ToolsConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10])
        raise _fail(path, f"invalid value - {problems} (approval must be never | always | "
                          f"external_write_only)") from exc
    known_tools, known_agents = set(all_tools()), set(agent_types())
    for name in cfg.tools:
        if name not in known_tools:
            raise _fail(path, f"unknown tool '{name}' (known: {', '.join(sorted(known_tools))})")
    for agent, names in cfg.agents.items():
        if agent not in known_agents:
            raise _fail(path, f"unknown agent '{agent}' (known: {', '.join(sorted(known_agents))})")
        for name in names:
            if name not in known_tools:
                raise _fail(path, f"agents.{agent}: unknown tool '{name}'")
    return cfg


@lru_cache
def get_tools_config() -> ToolsConfig | None:
    return load_tools_config(get_settings())


def disabled_reason(tool: Tool, cfg: ToolsConfig | None, settings: Settings) -> str | None:
    entry = cfg.entry(tool.name) if cfg else None
    if entry is None:
        return "not listed in tools.yaml"
    if not entry.enabled:
        return "disabled in tools.yaml"
    return tool.available(entry.options(), settings)


def effective_tools(agent_type: str, allowed: tuple[str, ...], cfg: ToolsConfig | None,
                    settings: Settings | None = None) -> list[str]:
    """The agent's class allowlist ∩ the config allowlist ∩ enabled and usable tools (config wins)."""
    if cfg is None:
        return []
    from app.tools.registry import all_tools

    settings = settings or get_settings()
    tools = all_tools()
    configured = cfg.agents.get(agent_type, [])
    return [n for n in allowed if n in configured and n in tools
            and disabled_reason(tools[n], cfg, settings) is None]


def approval_mode(tool: Tool, cfg: ToolsConfig | None) -> ApprovalMode:
    entry = cfg.entry(tool.name) if cfg else None
    if entry is not None and entry.approval is not None:
        return entry.approval
    return "always" if tool.side_effect == "external_write" else "never"


class CredentialResolver:
    """Resolves a credential NAME (what the agent passes) to request headers. Secrets come from env."""

    def __init__(self, cfg: ToolsConfig | None, environ: Mapping[str, str] | None = None) -> None:
        self._specs = cfg.credentials if cfg else {}
        self._env = os.environ if environ is None else environ

    def headers(self, name: str) -> dict[str, str]:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolError(f"unknown credential '{name}' (configured: {', '.join(sorted(self._specs)) or 'none'})")
        secret = self._env.get(spec.env)
        if not secret:
            raise ToolError(f"credential '{name}' is not set (environment variable {spec.env} is empty)")
        return {spec.header: spec.format.format(secret=secret)}

    def secret_values(self) -> list[str]:
        return [v for spec in self._specs.values() if (v := self._env.get(spec.env))]
