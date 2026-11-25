"""Agent contract. Agents produce content only: they never touch the DB or change statuses."""

from __future__ import annotations

from abc import ABC
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.agents.prompts import build_task_prompt
from app.config import get_settings
from app.llm.base import LLMClient, LLMResponse, Source


class AgentInput(BaseModel):
    workflow_goal: str
    workflow_id: str | None = None  # side-effect agents (publisher) key their idempotency on it
    task_key: str
    task_name: str
    task_description: str = ""
    task_input: dict[str, Any] = Field(default_factory=dict)
    dependency_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AgentResult(BaseModel):
    agent_type: str
    output: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    sources: list[Source] = Field(default_factory=list)


class BaseAgent(ABC):
    agent_type: ClassVar[str]
    description: ClassVar[str]
    system_prompt: ClassVar[str]
    output_model: ClassVar[type[BaseModel]]
    use_search: ClassVar[bool] = False
    # False for agents that must never appear as a task in a workflow (e.g. the planner).
    assignable: ClassVar[bool] = True
    # True = tasks for this agent ALWAYS require human approval (enforced in code, Part 6 adds such agents).
    sensitive: ClassVar[bool] = False
    # Explicit Gemini thinking level; None = WORKER_THINKING_LEVEL from config.
    thinking_level: ClassVar[str | None] = None

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.settings = get_settings()
        # Set by the engine worker; only side-effect agents (publisher) use it. Content agents never do.
        self.session_factory: Any = None

    def build_system_prompt(self) -> str:
        """Override for prompts that depend on runtime state (e.g. the planner's agent catalog)."""
        return self.system_prompt

    def resolve_thinking_level(self) -> str:
        return self.thinking_level or self.settings.worker_thinking_level

    def resolve_model(self) -> str | None:
        """Worker agents use GEMINI_WORKER_MODEL (fast); None = the client's default model."""
        return self.settings.gemini_worker_model

    def build_user_prompt(self, agent_input: AgentInput) -> str:
        return build_task_prompt(
            workflow_goal=agent_input.workflow_goal,
            task_name=agent_input.task_name,
            task_description=agent_input.task_description,
            task_input=agent_input.task_input,
            dependency_outputs=agent_input.dependency_outputs,
            max_context_chars=self.settings.max_context_chars,
        )

    def postprocess(self, data: BaseModel, response: LLMResponse) -> BaseModel:
        """Hook for code-enforced fixes to model output (code decides control)."""
        return data

    async def run(self, agent_input: AgentInput) -> AgentResult:
        response = await self.llm.generate_structured(
            system_prompt=self.build_system_prompt(),
            user_prompt=self.build_user_prompt(agent_input),
            output_model=self.output_model,
            use_search=self.use_search and self.settings.enable_search_grounding,
            thinking_level=self.resolve_thinking_level(),
            model=self.resolve_model(),
        )
        data = self.postprocess(response.data, response)
        return AgentResult(
            agent_type=self.agent_type,
            output=data.model_dump(mode="json"),
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            sources=response.sources,
        )
