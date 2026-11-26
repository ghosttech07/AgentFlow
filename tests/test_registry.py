from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import BaseModel

from app.agents import BaseAgent, create_agent, get_agent_class, list_agents, register_agent
from app.agents import registry
from app.agents.registry import UnknownAgentError
from app.llm.fake_client import FakeLLMClient


@pytest.fixture
def clean_registry() -> Iterator[None]:
    saved = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def test_builtin_agents_registered() -> None:
    assert registry.agent_types() == ["analyst", "communicator", "planner", "publisher", "researcher", "reviewer", "writer"]
    # The planner exists but can never be assigned to a workflow task.
    assert registry.task_agent_types() == ["analyst", "communicator", "publisher", "researcher", "reviewer", "writer"]
    assert registry.sensitive_agent_types() == ["publisher"]


def test_list_agents_has_description_and_schema() -> None:
    researcher = next(a for a in list_agents() if a["agent_type"] == "researcher")
    assert researcher["description"]
    assert "key_findings" in researcher["output_schema"]["properties"]


def test_register_and_create(clean_registry: None) -> None:
    class EchoOut(BaseModel):
        text: str

    @register_agent
    class EchoAgent(BaseAgent):
        agent_type = "echo"
        description = "echo"
        system_prompt = "echo"
        output_model = EchoOut

    assert get_agent_class("echo") is EchoAgent
    agent = create_agent("echo", FakeLLMClient())
    assert isinstance(agent, EchoAgent)
    assert "echo" in registry.agent_types()


def test_register_rejects_incomplete_or_duplicate(clean_registry: None) -> None:
    with pytest.raises(TypeError):
        register_agent(type("Broken", (BaseAgent,), {"agent_type": "broken"}))
    with pytest.raises(ValueError, match="already registered"):
        register_agent(type("Dup", (get_agent_class("writer"),), {}))


def test_unknown_agent_type() -> None:
    with pytest.raises(UnknownAgentError, match="Valid agent types: analyst"):
        get_agent_class("nope")
