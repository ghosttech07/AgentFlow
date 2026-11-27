"""Policy-as-code (Part 11): `config/policies.yaml`, validated at startup, evaluated by code.

    rules:
      - name: publish_needs_two_approvers
        when: {agent_type: publisher}
        approvals: 2
        approver_role: approver
      - name: email_outside_company
        when: {tool: send_email, recipient_not_in_domains: [example.com]}
        require_approval: true
      - name: marketing_no_webhooks
        when: {tool: webhook_post, team: marketing}
        deny: true

`when` keys (all must match): agent_type, task_key, tool, team, side_effect, recipient_not_in_domains.
Effects: require_approval (true only), approvals (>= 1, DISTINCT people), approver_role, deny (tools only).

SAFETY FLOOR (code; no policy file, role or API call can switch these off):
  1. Sensitive agents (publisher) always need an approval.
  2. A tainted attempt's external write always needs an approval.
  3. Policies only ADD requirements: `require_approval: false` is rejected; the strictest matching rule wins.
  4. Only users with role approver or admin can approve; a lower approver_role is raised to approver.
  5. An approval needing 2+ people: each person votes once and the workflow's creator cannot vote (four eyes).
  6. A rejection by any eligible approver rejects (one veto).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.governance.principal import ROLE_RANK

logger = logging.getLogger(__name__)

SAFETY_FLOOR = (
    "sensitive agents always need an approval",
    "a tainted attempt's external write always needs an approval",
    "policies can only add requirements (the strictest matching rule wins)",
    "only approvers and admins can approve",
    "approvals needing 2+ people: one vote per person, the workflow creator cannot vote",
    "one rejection by an eligible approver rejects",
)


class PolicyConfigError(Exception):
    """policies.yaml is invalid: the app refuses to start."""


class When(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_type: str | None = None
    task_key: str | None = None  # glob, e.g. "publish_*"
    tool: str | None = None
    team: str | None = None
    side_effect: Literal["none", "internal_write", "external_read", "external_write"] | None = None
    recipient_not_in_domains: list[str] | None = None


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    when: When = Field(default_factory=When)
    require_approval: Literal[True] | None = None  # floor 3: never false
    approvals: int | None = Field(default=None, ge=1, le=10)
    approver_role: Literal["viewer", "operator", "approver", "admin"] | None = None
    deny: bool = False

    @field_validator("approver_role")
    @classmethod
    def _floor(cls, v: str | None) -> str | None:  # floor 4
        return None if v is None else ("approver" if ROLE_RANK[v] < ROLE_RANK["approver"] else v)


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[Rule] = Field(default_factory=list)


@dataclass
class Requirement:
    require: bool = False
    approvals: int = 1
    approver_role: str | None = None
    deny: bool = False
    rules: list[str] = field(default_factory=list)

    def merge(self, r: Rule) -> None:
        self.rules.append(r.name)
        self.deny = self.deny or r.deny
        if r.require_approval or r.approvals or r.approver_role:
            self.require = True
        self.approvals = max(self.approvals, r.approvals or 1)
        if r.approver_role and (self.approver_role is None or ROLE_RANK[r.approver_role] > ROLE_RANK[self.approver_role]):
            self.approver_role = r.approver_role


class Policies:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules
        names = [r.name for r in rules]
        if len(names) != len(set(names)):
            raise PolicyConfigError("policy rule names must be unique")

    def _match(self, r: Rule, ctx: dict[str, Any]) -> bool:
        w = r.when
        if w.agent_type and w.agent_type != ctx.get("agent_type"):
            return False
        if w.task_key and not fnmatch.fnmatchcase(ctx.get("task_key") or "", w.task_key):
            return False
        if w.tool and w.tool != ctx.get("tool"):
            return False
        if w.team and w.team != ctx.get("team"):
            return False
        if w.side_effect and w.side_effect != ctx.get("side_effect"):
            return False
        if w.recipient_not_in_domains is not None:
            allowed = {d.lower() for d in w.recipient_not_in_domains}
            recipients = [str(x) for x in ctx.get("recipients") or []]
            if not any(x.rsplit("@", 1)[-1].lower() not in allowed for x in recipients):
                return False
        return True

    def evaluate(self, **ctx: Any) -> Requirement:
        req = Requirement()
        for r in self.rules:
            if self._match(r, ctx):
                req.merge(r)
        return req

    def task_rules(self) -> list[Rule]:
        """Rules that can apply to a whole task (no tool condition)."""
        return [r for r in self.rules if not r.when.tool and not r.when.side_effect
                and r.when.recipient_not_in_domains is None]


def load_policies(path: str | Path) -> Policies:
    p = Path(path)
    if not p.exists():
        return Policies([])
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return Policies(PolicyFile.model_validate(data).rules)
    except (yaml.YAMLError, ValidationError) as exc:
        raise PolicyConfigError(f"{p}: {exc}") from exc


@lru_cache
def _cached(path: str) -> Policies:
    return load_policies(path)


def get_policies() -> Policies:
    from app.config import get_settings

    return _cached(str(get_settings().policies_config_path))


def recipients_of(arguments: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("to", "cc", "bcc"):
        v = arguments.get(k)
        out += [v] if isinstance(v, str) else list(v or [])
    return out
