"""Tool contract (Part 9).

A tool is code. The LLM only PROPOSES a call (tool name + arguments); the tool loop validates the
arguments against `input_model`, decides approval, journals the call and then runs `execute`.
Tools never receive raw secrets in their arguments: credentials are resolved by name from config.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel

from app.config import Settings

if TYPE_CHECKING:
    from app.tools.config import CredentialResolver

# none = pure computation; internal_write = files in the workflow sandbox; external_read = network/DB reads;
# external_write = anything that changes the outside world (email, POST/PUT/DELETE, webhooks).
SideEffect = Literal["none", "internal_write", "external_read", "external_write"]
WRITE_EFFECTS: frozenset[str] = frozenset({"internal_write", "external_write"})


class ToolError(Exception):
    """A tool failed in a way the agent should hear about (returned to the LLM as the step result).

    Tool errors never crash the task: the model can adapt (another query, another URL, finish).
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.transient = transient


@dataclass
class ToolContext:
    workflow_id: uuid.UUID
    task_key: str
    attempt: int
    sandbox: Path
    idempotency_key: str
    settings: Settings
    options: dict[str, Any] = field(default_factory=dict)  # this tool's entry in tools.yaml
    credentials: CredentialResolver | None = None
    # Set by a human approver only (e.g. allow_recipient_override); never part of the LLM-visible arguments.
    approval_flags: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("app.tools"))


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]  # written for the LLM
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    side_effect: ClassVar[SideEffect]
    idempotent: ClassVar[bool] = False
    untrusted: ClassVar[bool] = False  # results come from the outside world (prompt-injection risk)
    supports_idempotency_key: ClassVar[bool] = False  # safe to re-run with the same key (outbox, files, webhook)
    timeout_seconds: ClassVar[float] = 30.0
    max_output_chars: ClassVar[int] = 4000

    def side_effect_for(self, args: BaseModel) -> SideEffect:
        """Most tools have one side effect; http_request depends on the method."""
        return self.side_effect

    def replay_safe(self, options: dict[str, Any]) -> bool:
        """True if re-executing after an unknown outcome cannot duplicate the effect."""
        return self.supports_idempotency_key

    def available(self, options: dict[str, Any], settings: Settings) -> str | None:
        """None = usable; otherwise the reason it is disabled (logged once at startup, shown in GET /tools)."""
        return None

    @abstractmethod
    async def execute(self, args: BaseModel, ctx: ToolContext) -> BaseModel: ...
