"""PII protection for outbound LLM calls (Part 12). Code decides; the LLM never sees what it must not see.

Per call and per provider locality (ollama = local; every other provider = external, PROVIDER_LOCALITY overrides):
  1. detect PII + secrets in the system and user prompts;
  2. modes per type = the strictest of: the config defaults (PRIVACY_DEFAULT_EXTERNAL, external providers only)
     and the policy decision point `llm_call` {provider, model, provider_locality, pii_types_found, team, agent_type};
  3. SAFETY FLOOR: secrets never go to an external provider (local_only), whatever the config or policy says;
  4. local_only / block -> this entry is skipped (the router tries the next one; none left -> privacy_blocked);
     tokenize -> <EMAIL_1> placeholders from the workflow's encrypted vault (stable per value);
     mask -> [EMAIL]; allow -> unchanged.
Re-hydration (`rehydrate`) happens only where a real value is needed after the checks (an approved send_email
recipient, a reveal by a user with privacy.reveal).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.privacy import crypto
from app.privacy.detectors import Finding, detect

ORDER = {"allow": 0, "tokenize": 1, "mask": 2, "local_only": 3, "block": 4}
TOKEN_RE = re.compile(r"<([A-Z]+)_(\d+)>")


def stricter(a: str, b: str) -> str:
    return a if ORDER.get(a, 0) >= ORDER.get(b, 0) else b


def parse_modes(spec: str) -> dict[str, str]:
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            t, m = part.split("=", 1)
            if m.strip() in ORDER:
                out[t.strip().upper()] = m.strip()
    return out


def locality_of(provider: str) -> str:
    return "local" if provider == "ollama" else "external"


@dataclass
class Protected:
    system_prompt: str
    user_prompt: str
    skip_reason: str | None = None
    types: list[str] = field(default_factory=list)
    actions: dict[str, str] = field(default_factory=dict)  # type -> mode applied
    counts: dict[str, int] = field(default_factory=dict)
    rules: list[str] = field(default_factory=list)


async def _token_for(session: AsyncSession, workflow_id: uuid.UUID, f: Finding, key: bytes) -> str:
    from app.models.privacy import PiiToken

    h = crypto.value_hmac(f.value)
    existing = await session.scalar(select(PiiToken.token).where(PiiToken.workflow_id == workflow_id,
                                                                 PiiToken.value_hmac == h))
    if existing:
        return existing
    n = await session.scalar(select(func.count()).select_from(PiiToken).where(
        PiiToken.workflow_id == workflow_id, PiiToken.pii_type == f.type))
    token = f"<{f.type}_{int(n or 0) + 1}>"
    nonce, ct = crypto.encrypt(key, f.value, f"{workflow_id}:vault")
    session.add(PiiToken(workflow_id=workflow_id, token=token, pii_type=f.type, value_hmac=h, nonce=nonce,
                         ciphertext=ct))
    await session.flush()
    return token


async def _apply(session: AsyncSession | None, workflow_id: uuid.UUID | None, text_in: str, modes: dict[str, str],
                 counts: dict[str, int], key: bytes | None) -> str:
    out, last = [], 0
    for f in detect(text_in):
        mode = modes.get(f.type, "allow")
        if mode == "allow":
            continue
        if mode == "tokenize" and (session is None or workflow_id is None or key is None):
            mode = "mask"  # no workflow vault (e.g. Quick Ask): masking is the safe fallback
        out.append(text_in[last:f.start])
        out.append(await _token_for(session, workflow_id, f, key) if mode == "tokenize" else f"[{f.type}]")
        counts[f"{f.type}:{mode}"] = counts.get(f"{f.type}:{mode}", 0) + 1
        last = f.end
    out.append(text_in[last:])
    return "".join(out)


async def protect(session_factory: async_sessionmaker[AsyncSession] | None, *, workflow_id: uuid.UUID | None,
                  team: str | None, agent_type: str | None, provider: str, model: str, system_prompt: str,
                  user_prompt: str, settings: Any) -> Protected:
    from app.governance.policy import get_policies

    locality = locality_of(provider)
    found = detect(system_prompt) + detect(user_prompt)
    types = sorted({f.type for f in found})
    if not types:
        return Protected(system_prompt, user_prompt)
    modes = dict(parse_modes(settings.privacy_default_external)) if locality == "external" else {}
    req = get_policies().evaluate(scope="llm", provider=provider, model=model, provider_locality=locality,
                                  pii_types=types, team=team, agent_type=agent_type)
    for t, m in req.pii_modes.items():
        modes[t] = stricter(modes.get(t, "allow"), m)
    if req.only_local:
        for t in types:
            modes[t] = stricter(modes.get(t, "allow"), "local_only")
    if "SECRET" in types:  # safety floor
        modes["SECRET"] = stricter(modes.get("SECRET", "allow"), "local_only" if locality == "external" else "allow")
    if locality == "local":  # a local model may see local_only data; block still blocks
        modes = {t: ("allow" if m == "local_only" else m) for t, m in modes.items()}
    actions = {t: modes.get(t, "allow") for t in types}
    blocking = sorted(t for t in types if actions[t] in ("local_only", "block"))
    if blocking:
        why = "external provider" if any(actions[t] == "local_only" for t in blocking) else "blocked by policy"
        return Protected(system_prompt, user_prompt, skip_reason=f"{', '.join(blocking)} may not go to an {why}"
                         if why == "external provider" else f"{', '.join(blocking)}: {why}", types=types,
                         actions=actions, rules=req.rules)
    counts: dict[str, int] = {}
    if all(m == "allow" for m in actions.values()):
        return Protected(system_prompt, user_prompt, types=types, actions=actions, rules=req.rules)
    if session_factory is None or workflow_id is None:
        sp = await _apply(None, None, system_prompt, modes, counts, None)
        up = await _apply(None, None, user_prompt, modes, counts, None)
        return Protected(sp, up, types=types, actions=actions, counts=counts, rules=req.rules)
    async with session_factory() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"vault:{workflow_id}"})
        key = await crypto.data_key(s, workflow_id)
        sp = await _apply(s, workflow_id, system_prompt, modes, counts, key)
        up = await _apply(s, workflow_id, user_prompt, modes, counts, key)
        await s.commit()
    return Protected(sp, up, types=types, actions=actions, counts=counts, rules=req.rules)


async def rehydrate(session: AsyncSession, workflow_id: uuid.UUID, value: Any) -> Any:
    """Replace <TYPE_n> placeholders with the real values (only call this after the checks)."""
    from app.models.privacy import PiiToken

    rows = {r.token: r for r in await session.scalars(select(PiiToken).where(PiiToken.workflow_id == workflow_id))}
    if not rows:
        return value
    key = await crypto.data_key(session, workflow_id, create=False)

    def sub(s: str) -> str:
        return TOKEN_RE.sub(lambda m: crypto.decrypt(key, rows[m.group(0)].nonce, rows[m.group(0)].ciphertext,
                                                     f"{workflow_id}:vault") if m.group(0) in rows else m.group(0), s)

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return sub(v)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value)


def summary(counts: dict[str, int], skipped: list[str] | None = None) -> str:
    """'2 emails tokenized, 1 Aadhaar kept local' (no values)."""
    parts = []
    for k, n in sorted(counts.items()):
        t, mode = k.split(":")
        verb = {"tokenize": "tokenized", "mask": "masked"}.get(mode, mode)
        parts.append(f"{n} {t.lower()}{'s' if n > 1 else ''} {verb}")
    parts += [f"{t.lower()} kept local" for t in skipped or []]
    return ", ".join(parts)
