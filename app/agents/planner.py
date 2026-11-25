"""Planner: turns a goal into a task DAG. The AI proposes; app.core.dag + planning_service decide."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import AgentInput, BaseAgent
from app.agents.prompts import today
from app.agents.registry import get_agent_class, register_agent, task_agent_types


class PlannedTask(BaseModel):
    key: str = Field(description="Unique lowercase snake_case id, e.g. 'research_india'.")
    name: str = Field(description="Short human-readable title.")
    description: str = Field(
        description="Detailed, self-contained instructions. The agent sees ONLY this, the task "
        "input and the outputs of the tasks it depends on."
    )
    agent_type: str = Field(description="Exactly one of the available agent types.")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description='Task parameters, e.g. {"max_findings": 5, "audience": "beginners", "length": "500 words"}.',
    )
    depends_on: list[str] = Field(
        default_factory=list, description="Keys of tasks whose OUTPUT this task needs."
    )
    requires_approval: bool = Field(default=False)
    rationale: str = Field(description="One short sentence: why this task exists.")


class Plan(BaseModel):
    plan_summary: str = Field(description="2-3 sentences describing the approach.")
    tasks: list[PlannedTask]


EXAMPLE_PLAN = {
    "plan_summary": "Research the two markets in parallel, compare them, write the report, then review it.",
    "tasks": [
        {"key": "research_market_a", "name": "Research market A", "agent_type": "researcher",
         "description": "Collect 2023-2025 facts on ... for market A: ...", "input": {"max_findings": 6},
         "depends_on": [], "requires_approval": False, "rationale": "Facts for A."},
        {"key": "research_market_b", "name": "Research market B", "agent_type": "researcher",
         "description": "Collect 2023-2025 facts on ... for market B: ...", "input": {"max_findings": 6},
         "depends_on": [], "requires_approval": False, "rationale": "Facts for B, independent of A."},
        {"key": "compare_markets", "name": "Compare markets", "agent_type": "analyst",
         "description": "Compare A and B on ...", "input": {},
         "depends_on": ["research_market_a", "research_market_b"], "requires_approval": False,
         "rationale": "Turns both research results into insights."},
        {"key": "write_report", "name": "Write report", "agent_type": "writer",
         "description": "Write a ... report for ... using the comparison and research.",
         "input": {"audience": "beginners", "length": "500 words"},
         "depends_on": ["compare_markets", "research_market_a", "research_market_b"],
         "requires_approval": False, "rationale": "The final deliverable."},
        {"key": "review_report", "name": "Review report", "agent_type": "reviewer",
         "description": "Check the report against the research for accuracy and completeness.",
         "input": {}, "depends_on": ["write_report", "research_market_a", "research_market_b"],
         "requires_approval": False, "rationale": "Quality gate on the deliverable."},
    ],
}


@register_agent
class PlannerAgent(BaseAgent):
    agent_type = "planner"
    description = "Breaks a workflow goal into a dependency graph of tasks for the other agents."
    output_model = Plan
    assignable = False  # never a task in a plan; rejected by app.core.dag.validate_plan
    system_prompt = ""  # built dynamically: see build_system_prompt

    def resolve_thinking_level(self) -> str:
        return self.thinking_level or self.settings.planner_thinking_level

    def resolve_model(self) -> str | None:
        return None  # planning quality matters most: always the main GEMINI_MODEL

    def build_system_prompt(self) -> str:
        catalog = "\n".join(_describe_agent(t) for t in task_agent_types())
        return f"""You are the planner of a multi-agent system. Turn the user's goal into a task graph that the agents below execute.

AVAILABLE AGENTS (use these agent_type values exactly; no others exist):
{catalog}

RULES
1. Use the FEWEST tasks that fully achieve the goal. Never add tasks nobody needs.
2. Split independent research into PARALLEL tasks: tasks that do not need each other's output must NOT depend on each other.
3. A task depends on another ONLY if it needs that task's output. Never add ordering-only dependencies.
4. End with one final deliverable task (usually writer), optionally followed by a reviewer that depends on the deliverable and on the research it checks.
5. Keys: unique, lowercase snake_case (a-z, 0-9, _), start with a letter, max 50 chars.
6. At most {self.settings.max_plan_tasks} tasks. No cycles, no self-dependencies, depends_on only lists keys from your plan.
7. Each description must be detailed enough for the agent to do the task alone: what to cover, scope, time period, region.
8. Put user requirements (audience, length, tone, format, limits) into the "input" of the task that needs them.
   Every researcher task gets "search_queries" in its input: 2-3 short web search queries (3-8 words each, include the year for current data, e.g. "India EV sales 2026"). Researchers read live web pages for these queries.
9. Set requires_approval=true for any task that publishes, sends, deletes, spends money, or contacts people. Tasks of agents marked [SENSITIVE] always require approval.
10. Use the publisher ONLY when the goal explicitly asks to publish, post or share the result. It is always the LAST task and depends on the reviewer and on the writer task whose content it publishes. Never add it otherwise.
11. Fill every field, including a one-sentence rationale per task.

EXAMPLE OF A GOOD PLAN (parallel research -> analysis -> writing -> review):
{json.dumps(EXAMPLE_PLAN, indent=2)}"""

    def build_user_prompt(self, agent_input: AgentInput) -> str:
        data = agent_input.task_input
        prompt = f"## TODAY\n{today()}\n\n## GOAL\n{agent_input.workflow_goal}\n"
        if data.get("context"):
            prompt += f"\n## WORKFLOW CONTEXT (JSON)\n{json.dumps(data['context'], ensure_ascii=False, indent=2)}\n"
        if data.get("constraints"):
            prompt += f"\n## CONSTRAINTS (JSON)\n{json.dumps(data['constraints'], ensure_ascii=False, indent=2)}\n"
        if data.get("validation_errors"):
            prompt += (
                "\n## YOUR PREVIOUS PLAN WAS REJECTED\n"
                f"Previous plan (JSON):\n{json.dumps(data.get('previous_plan'), ensure_ascii=False, indent=2)}\n\n"
                "Validation errors (fix ALL of them):\n"
                + "\n".join(f"- {e}" for e in data["validation_errors"])
                + "\n\nReturn a complete corrected plan.\n"
            )
        return prompt


def _describe_agent(agent_type: str) -> str:
    cls = get_agent_class(agent_type)
    flag = " [SENSITIVE: always requires approval]" if cls.sensitive else ""
    return f"- {agent_type}{flag}: {cls.description} Output fields: {', '.join(cls.output_model.model_fields)}."
