"""Background planning job for workflows created with run=true."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import WorkflowStatus
from app.llm.base import LLMClient
from app.models.workflow import Workflow
from app.services import planning_service
from app.services.workflow_service import start_workflow

logger = logging.getLogger(__name__)


async def plan_and_start(
    session_factory: async_sessionmaker[AsyncSession], llm: LLMClient, workflow_id: uuid.UUID
) -> None:
    """The workflow is already PLANNING (claimed by pickup_run_requests). Plan it, then start it.

    On planning failure the workflow ends FAILED exactly as in Part 3, and run_requested is
    cleared so a later manual re-plan does not auto-start unexpectedly.
    """
    async with session_factory() as session:
        workflow = await session.get(Workflow, workflow_id)
        if workflow is None:
            return
        try:
            await planning_service.complete_planning(
                session, workflow, llm, planning_service.stored_constraints(workflow)
            )
        except Exception as exc:
            logger.warning("run.planning_failed workflow=%s error=%s", workflow_id, type(exc).__name__)
            workflow.run_requested = False
            await session.commit()
            return
        if workflow.status != WorkflowStatus.PENDING:  # cancelled while planning: plan was discarded
            return
        await start_workflow(session, workflow_id)
