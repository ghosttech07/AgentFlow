"""Specialist agents. Importing this package registers every agent in the registry."""

from app.agents import analyst, planner, publisher, researcher, reviewer, writer  # noqa: F401  (registration)
from app.agents.base import AgentInput, AgentResult, BaseAgent
from app.agents.registry import create_agent, get_agent_class, list_agents, register_agent

__all__ = [
    "AgentInput",
    "AgentResult",
    "BaseAgent",
    "create_agent",
    "get_agent_class",
    "list_agents",
    "register_agent",
]
