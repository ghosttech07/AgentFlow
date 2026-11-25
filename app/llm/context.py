"""Which workflow/task an LLM call belongs to, without agents knowing about budgets.

The worker (per task) and the planning path (per workflow) set it; asyncio tasks inherit it, so the
router can record usage rows and enforce the workflow budget right before and after each real call.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CallContext:
    workflow_id: uuid.UUID | None
    task_id: uuid.UUID | None = None  # None = planner call (counted, never blocked before the call)


call_context: ContextVar[CallContext | None] = ContextVar("agentflow_call_context", default=None)


@contextmanager
def use_context(ctx: CallContext) -> Iterator[None]:
    token = call_context.set(ctx)
    try:
        yield
    finally:
        call_context.reset(token)
