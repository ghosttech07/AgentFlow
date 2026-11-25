"""Agent registry: agent_type -> agent class.

Every agent is registered here, including the planner. `task_agent_types()` lists only the
agents a workflow task may use (`assignable = True`); the planner is not one of them.
"""

from __future__ import annotations

from typing import Any, TypeVar

from app.agents.base import BaseAgent
from app.core.errors import NotFoundError
from app.llm.base import LLMClient

A = TypeVar("A", bound=type[BaseAgent])

_REGISTRY: dict[str, type[BaseAgent]] = {}


class UnknownAgentError(NotFoundError):
    code = "unknown_agent"


def register_agent(cls: A) -> A:
    for attr in ("agent_type", "description", "system_prompt", "output_model"):
        if not hasattr(cls, attr):
            raise TypeError(f"{cls.__name__} must define class attribute '{attr}'")
    if cls.agent_type in _REGISTRY and _REGISTRY[cls.agent_type] is not cls:
        raise ValueError(f"Agent type '{cls.agent_type}' is already registered")
    _REGISTRY[cls.agent_type] = cls
    return cls


def agent_types() -> list[str]:
    return sorted(_REGISTRY)


def task_agent_types() -> list[str]:
    """Agent types that may be assigned to workflow tasks."""
    return sorted(t for t, cls in _REGISTRY.items() if cls.assignable)


def sensitive_agent_types() -> list[str]:
    """Agent types whose tasks must always require human approval."""
    return sorted(t for t, cls in _REGISTRY.items() if cls.sensitive)


def get_agent_class(agent_type: str) -> type[BaseAgent]:
    try:
        return _REGISTRY[agent_type]
    except KeyError:
        raise UnknownAgentError(
            f"Unknown agent type '{agent_type}'. Valid agent types: {', '.join(agent_types())}"
        ) from None


def create_agent(agent_type: str, llm_client: LLMClient) -> BaseAgent:
    return get_agent_class(agent_type)(llm_client)


def list_agents() -> list[dict[str, Any]]:
    return [
        {
            "agent_type": cls.agent_type,
            "description": cls.description,
            "assignable": cls.assignable,
            "sensitive": cls.sensitive,
            "output_schema": cls.output_model.model_json_schema(),
        }
        for _, cls in sorted(_REGISTRY.items())
    ]
