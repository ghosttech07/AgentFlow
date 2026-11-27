"""Agent endpoints. The run endpoint is a standalone dev tool: no tasks, no workflows, no DB."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents import AgentInput, AgentResult, create_agent, list_agents
from app.api.auth import require_creator, require_viewer
from app.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.llm import LLMClient, get_llm_client
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_viewer)])


class AgentInfo(BaseModel):
    agent_type: str
    description: str
    assignable: bool  # False = cannot be used as a workflow task (e.g. planner)
    sensitive: bool  # True = its tasks always require human approval
    output_schema: dict[str, Any]


class AgentRunRequest(BaseModel):
    workflow_goal: str = Field(min_length=1)
    task_name: str = Field(min_length=1)
    task_description: str = ""
    task_input: dict[str, Any] = Field(default_factory=dict)
    dependency_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.get("", response_model=list[AgentInfo])
async def get_agents() -> list[dict[str, Any]]:
    return list_agents()


@router.post(
    "/{agent_type}/run",
    dependencies=[Depends(require_creator)],  # spends LLM quota
    response_model=AgentResult,
    responses={
        404: {"model": ErrorResponse, "description": "Unknown agent (or disabled in production)"},
        502: {"model": ErrorResponse, "description": "LLM rejected the request or returned bad output"},
        503: {"model": ErrorResponse, "description": "LLM temporarily unavailable"},
    },
)
async def run_agent(
    agent_type: str,
    body: AgentRunRequest,
    llm: LLMClient = Depends(get_llm_client),
    settings: Settings = Depends(get_settings),
) -> AgentResult:
    if settings.app_env == "production":
        raise NotFoundError("Agent run endpoint is disabled in production")
    agent = create_agent(agent_type, llm)
    return await agent.run(AgentInput(task_key="adhoc", **body.model_dump()))
