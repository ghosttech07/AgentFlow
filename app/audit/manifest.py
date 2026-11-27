"""Reproducibility records (Part 12): the prompt registry, per-call manifests, encrypted content, run manifests.

- prompt_templates: every agent's system prompt template, by content hash (registered at startup and on first use;
  a changed prompt = a new hash).
- llm_calls.manifest: provider, model (+ version if returned), parameters, prompt_template_hash,
  rendered_prompt_hash (of exactly what was sent, after PII protection), response_hash, usage, latency, route
  position/fallback, PII actions.
- stored_contents: the prompt/response, AES-GCM encrypted with the workflow key (AUDIT_STORE_CONTENT).
- workflows.repro: code version + hashes of models.yaml / tools.yaml / policies.yaml / pricing.yaml.
- task_attempts.manifest: input hash, dependency output hashes, memory ids, tool result hashes, llm call ids, output
  hash; its digest goes into the audit chain (task.manifest_recorded).
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.canonical import digest, sha256_hex
from app.models.privacy import PromptTemplate, StoredContent

logger = logging.getLogger(__name__)
_registered: set[str] = set()
ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def code_version() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def file_hash(path: Path | None) -> str | None:
    try:
        return sha256_hex(Path(path).read_bytes()) if path and Path(path).exists() else None
    except OSError:
        return None


def repro_snapshot() -> dict[str, Any]:
    from app.config import get_settings

    s = get_settings()
    return {"code_version": code_version(), "models_yaml": file_hash(s.models_config_path),
            "tools_yaml": file_hash(s.tools_config_path), "policies_yaml": file_hash(s.policies_config_path),
            "pricing_yaml": file_hash(s.pricing_config_path), "store_content": s.audit_store_content}


def template_of(role: str | None) -> str | None:
    """The agent's prompt TEMPLATE (class attribute), not the rendered prompt."""
    if not role:
        return None
    try:
        from app.agents.registry import get_agent_class

        cls = get_agent_class(role)
    except Exception:  # noqa: BLE001 - unknown role (e.g. "ask", "judge")
        return None
    return getattr(cls, "system_prompt", None) if isinstance(getattr(cls, "system_prompt", None), str) else None


async def register(session_factory: async_sessionmaker[AsyncSession], agent_type: str, name: str, text: str) -> str:
    h = sha256_hex(text)
    if h in _registered:
        return h
    async with session_factory() as s:
        await s.execute(insert(PromptTemplate).values(hash=h, agent_type=agent_type[:64], name=name[:128], text=text)
                        .on_conflict_do_nothing())
        await s.commit()
    _registered.add(h)
    return h


async def register_all(session_factory: async_sessionmaker[AsyncSession]) -> int:
    from app.agents.registry import list_agents

    n = 0
    for info in list_agents():
        text = template_of(info["agent_type"] if isinstance(info, dict) else info.agent_type)
        if text:
            await register(session_factory, info["agent_type"] if isinstance(info, dict) else info.agent_type,
                           "system", text)
            n += 1
    return n


async def store(session_factory: async_sessionmaker[AsyncSession], workflow_id: uuid.UUID, llm_call_id: int | None,
                kind: str, content: str, mode: str) -> None:
    if mode == "hash_only":
        return
    from app.privacy import crypto
    from app.privacy.redact import mask_text

    plain = mask_text(content) if mode == "redacted" else content
    async with session_factory() as s:
        key = await crypto.data_key(s, workflow_id)
        nonce, ct = crypto.encrypt(key, plain, f"{workflow_id}:{kind}")
        s.add(StoredContent(workflow_id=workflow_id, llm_call_id=llm_call_id, kind=kind, mode=mode,
                            sha256=sha256_hex(plain), nonce=nonce, ciphertext=ct))
        await s.commit()


async def read_content(session: AsyncSession, workflow_id: uuid.UUID) -> list[dict[str, Any]]:
    """Decrypted stored content of a workflow (raises crypto.ShreddedError once the key is gone)."""
    from app.privacy import crypto

    rows = list(await session.scalars(select(StoredContent).where(StoredContent.workflow_id == workflow_id)
                                      .order_by(StoredContent.created_at)))
    if not rows:
        return []
    key = await crypto.data_key(session, workflow_id, create=False)
    return [{"llm_call_id": r.llm_call_id, "kind": r.kind, "mode": r.mode, "sha256": r.sha256,
             "text": crypto.decrypt(key, r.nonce, r.ciphertext, f"{workflow_id}:{r.kind}")} for r in rows]


def attempt_manifest(agent_input: Any, output: Any, llm_call_ids: list[int], tool_hashes: list[str]) -> dict[str, Any]:
    deps = getattr(agent_input, "dependency_outputs", {}) or {}
    return {"input_hash": digest(getattr(agent_input, "task_input", None) or {}),
            "dependency_output_hashes": {k: digest(v) for k, v in sorted(deps.items())},
            "memory_ids": list(getattr(agent_input, "memory_ids", []) or []),
            "tool_result_hashes": tool_hashes, "llm_call_ids": llm_call_ids,
            "output_hash": digest(output) if output is not None else None}


async def build_attempt_manifest(session: AsyncSession, task: Any) -> dict[str, Any]:
    """The run manifest of the task's current attempt, from the DB (inputs, LLM calls, tool results, output)."""
    from sqlalchemy.orm import selectinload

    from app.models.attempt import TaskAttempt
    from app.models.task import Task
    from app.models.tool_call import ToolCall
    from app.models.usage import LLMCall

    attempt = await session.scalar(select(TaskAttempt).where(TaskAttempt.task_id == task.id)
                                   .order_by(TaskAttempt.attempt_number.desc()).limit(1))
    since = attempt.started_at if attempt is not None else None
    deps = list((await session.scalar(select(Task).where(Task.id == task.id)
                                      .options(selectinload(Task.dependencies)))).dependencies)
    calls = list(await session.scalars(select(LLMCall).where(LLMCall.task_id == task.id,
                                                             *([LLMCall.created_at >= since] if since else []))
                                       .order_by(LLMCall.id)))
    tools = list(await session.scalars(select(ToolCall).where(ToolCall.task_id == task.id).order_by(ToolCall.created_at)))
    return {"task_key": task.key, "attempt": attempt.attempt_number if attempt else None,
            "agent_type": task.agent_type, "input_hash": digest(task.input or {}),
            "dependency_output_hashes": {d.key: digest(d.output) for d in sorted(deps, key=lambda d: d.key)},
            "memory_ids": list(((task.metrics or {}).get("memories_used")) or []),
            "llm_calls": [{"id": c.id, **(c.manifest or {"provider": c.provider, "model": c.model})} for c in calls
                          if c.outcome == "succeeded"],
            "tool_result_hashes": [{"tool": t.tool, "status": t.status, "result_hash": digest(t.result)}
                                   for t in tools],
            "output_hash": digest(task.output) if task.output is not None else None}
