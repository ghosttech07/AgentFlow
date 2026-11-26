"""Tool registry, like the agent registry: `@register_tool` on a Tool subclass registers one instance."""

from __future__ import annotations

from typing import TypeVar

from app.tools.base import Tool

_TOOLS: dict[str, Tool] = {}
T = TypeVar("T", bound=type[Tool])


def register_tool(cls: T) -> T:
    if cls.name in _TOOLS and type(_TOOLS[cls.name]) is not cls:
        raise ValueError(f"Tool name {cls.name!r} is already registered by {type(_TOOLS[cls.name]).__name__}")
    _TOOLS[cls.name] = cls()
    return cls


def unregister_tool(name: str) -> None:  # tests only
    _TOOLS.pop(name, None)


def get_tool(name: str) -> Tool:
    return _TOOLS[name]


def all_tools() -> dict[str, Tool]:
    return dict(sorted(_TOOLS.items()))
