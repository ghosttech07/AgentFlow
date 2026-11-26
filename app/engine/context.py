"""Build an agent's input from the database (never from engine memory)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents import AgentInput
from app.core.errors import NotFoundError
from app.models.task import Task


async def build_agent_input(session: AsyncSession, task_id: uuid.UUID) -> tuple[Task, AgentInput]:
    """Goal + task fields + outputs of the task's DIRECT dependencies, keyed by task key."""
    task = await session.scalar(
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.dependencies), selectinload(Task.workflow))
    )
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    from app.services import memory_service

    memories = await memory_service.for_task(session, task.workflow, task)
    return task, AgentInput(
        prior_knowledge=memory_service.as_prompt(memories) if memories else None,
        memory_ids=[str(m.id) for m in memories],
        memory_tainted=any(m.tainted for m in memories),
        memory_private=any(m.private for m in memories),
        memory_sources=[s.get("url") for m in memories if m.kind == "fact" for s in (m.sources or []) if s.get("url")],
        workflow_goal=task.workflow.goal,
        workflow_id=str(task.workflow_id),
        task_key=task.key,
        task_name=task.name,
        task_description=task.description,
        task_input=task.input,
        dependency_outputs={dep.key: dep.output or {} for dep in task.dependencies},
    )
