"""Publisher: the first sensitive agent. Deterministic (no LLM call), so it uses no model quota.

It publishes the writer's content as a Markdown file and records a `publications` row.
Exception to "agents never touch the DB": this side effect IS the agent's job, and it is made
idempotent with idempotency_key = "<workflow_id>:<task_key>" (stable across attempts).

Ordering (why a crash or retry never publishes twice):
  1. Row with this key exists?  -> return it, already_published=true (nothing written).
  2. Write the file atomically (temp file + os.replace). Path and content are deterministic, so
     a rewrite after a crash replaces the same file with the same bytes: never a second file.
  3. INSERT ... ON CONFLICT (idempotency_key) DO NOTHING, then read the row.
A crash between 2 and 3 leaves a file and no row; the retry rewrites the same file and inserts the
row once. The reverse order (row first) could leave a row claiming a publication whose file was
never written, which is worse than a file nobody has recorded yet.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import AgentInput, AgentResult, BaseAgent
from app.agents.registry import register_agent
from app.llm.errors import LLMPermanentError
from app.models.approval import Publication

MODEL_NAME = "publisher (no LLM)"


class PublishOutput(BaseModel):
    published: bool
    location: str
    published_at: str
    idempotency_key: str
    already_published: bool


@register_agent
class PublisherAgent(BaseAgent):
    agent_type = "publisher"
    description = ("Publishes the final written content (title + Markdown from the writer task it "
                   "depends on). Deterministic, no AI. Only when the goal asks to publish/post/share.")
    system_prompt = ""  # never calls the LLM
    output_model = PublishOutput
    sensitive = True  # every publish needs explicit human approval

    async def run(self, agent_input: AgentInput) -> AgentResult:
        started = time.perf_counter()
        if not agent_input.workflow_id:
            raise LLMPermanentError("Publisher needs the workflow id (run it inside a workflow)")
        key = f"{agent_input.workflow_id}:{agent_input.task_key}"
        sf = self.session_factory or _default_session_factory()

        async with sf() as session:
            row = await _find(session, key)
        already = row is not None
        if row is None:
            path = self.settings.publish_dir / f"{agent_input.workflow_id}__{agent_input.task_key}.md"
            _atomic_write(path, render_document(agent_input))
            row = await _record(sf, uuid.UUID(agent_input.workflow_id), agent_input.task_key, key, str(path))

        output = PublishOutput(published=True, location=row.location, published_at=row.published_at.isoformat(),
                               idempotency_key=key, already_published=already)
        return AgentResult(agent_type=self.agent_type, output=output.model_dump(), model=MODEL_NAME,
                           input_tokens=0, output_tokens=0,
                           latency_ms=int((time.perf_counter() - started) * 1000))


def render_document(agent_input: AgentInput) -> str:
    """Title + content from the task input (approval overrides) or the writer's output."""
    data: dict[str, Any] = {}
    for output in agent_input.dependency_outputs.values():
        if isinstance(output.get("content"), str):
            data = output
            break
    title = agent_input.task_input.get("title") or data.get("title")
    content = agent_input.task_input.get("content") or data.get("content")
    if not content:
        raise LLMPermanentError("Nothing to publish: no dependency output has 'content'")
    # Deterministic (no timestamps): a rewrite after a crash produces identical bytes.
    front = [f"title: {title or 'Untitled'}", f"workflow_id: {agent_input.workflow_id}",
             f"task_key: {agent_input.task_key}"]
    if agent_input.task_input.get("note"):
        front.append(f"note: {agent_input.task_input['note']}")
    return "---\n" + "\n".join(front) + "\n---\n\n" + content.rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on the same filesystem: readers never see a half-written file


async def _find(session: AsyncSession, key: str) -> Publication | None:
    return await session.scalar(select(Publication).where(Publication.idempotency_key == key))


async def _record(sf: async_sessionmaker[AsyncSession], workflow_id: uuid.UUID, task_key: str, key: str,
                  location: str) -> Publication:
    async with sf() as session:
        await session.execute(
            pg_insert(Publication)
            .values(id=uuid.uuid4(), workflow_id=workflow_id, task_key=task_key, idempotency_key=key,
                    location=location)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        await session.commit()
        row = await _find(session, key)
        assert row is not None
        return row


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    from app.db.session import SessionLocal  # lazy: importing agents must not need a DB

    return SessionLocal
