"""Memory between workflows (Part 10): search, inspect, pin, delete (soft, audited), purge expired."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.db.session import get_session
from app.schemas.common import ErrorResponse
from app.services import memory_service

router = APIRouter(prefix="/memories", tags=["memory"])
NOT_FOUND = {404: {"model": ErrorResponse}}


@router.get("")
async def list_memories(q: str = Query(default="", max_length=500), kind: str | None = None,
                        namespace: str = "default", limit: int = Query(default=50, ge=1, le=200),
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Search memories (full text, ranked; pinned first). Empty q = newest first."""
    rows = await memory_service.search(session, q, namespace=namespace, k=limit, kind=kind)
    return {"items": [memory_service.to_dict(m) for m in rows]}


@router.get("/{memory_id}", responses=NOT_FOUND)
async def get_memory(memory_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return memory_service.to_dict(await memory_service.get(session, memory_id))


@router.post("/{memory_id}/pin", responses=NOT_FOUND, dependencies=[Depends(require_admin)])
async def pin_memory(memory_id: uuid.UUID, pinned: bool = True,
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return memory_service.to_dict(await memory_service.pin(session, memory_id, pinned))


@router.delete("/{memory_id}", status_code=204, responses=NOT_FOUND, dependencies=[Depends(require_admin)])
async def delete_memory(memory_id: uuid.UUID, actor: str = "admin", session: AsyncSession = Depends(get_session)) -> None:
    await memory_service.soft_delete(session, memory_id, actor)


@router.post("/purge-expired", dependencies=[Depends(require_admin)])
async def purge_expired(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return {"purged": await memory_service.purge_expired(session)}
