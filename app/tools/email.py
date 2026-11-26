"""send_email: external_write, approval always. Backends: outbox (default; nothing is really sent) | smtp.

Outbox writes outputs/outbox/<idempotency_key>.eml with deterministic bytes (Message-ID derived from the
key), so a replay rewrites the same file: exactly one email. SMTP has no idempotency on the wire, so an
unknown outcome always goes back to a human (replay_safe = False). Recipients must match the allowlist
unless the human approver explicitly allows an override (an approval flag, never an LLM argument).
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import smtplib
import tempfile
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.db.base import utcnow
from app.tools.base import Tool, ToolContext, ToolError
from app.tools.registry import register_tool


class SendEmailArgs(BaseModel):
    to: list[str] = Field(min_length=1, max_length=10, description="Recipient addresses.")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000, description="Plain-text body.")

    @field_validator("to")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        out = []
        for addr in value:
            addr = addr.strip().lower()
            if addr.count("@") != 1 or " " in addr or not addr.split("@")[1] or "\n" in addr:
                raise ValueError(f"not an email address: {addr!r}")
            out.append(addr)
        return out


class SendEmailResult(BaseModel):
    backend: str
    message_id: str
    recipients: list[str]
    subject: str
    location: str | None = None


def _check_recipients(to: list[str], patterns: list[str], override: bool) -> None:
    bad = [a for a in to if not any(fnmatch.fnmatch(a, p.lower()) for p in patterns)]
    if bad and not override:
        raise ToolError(f"recipient(s) not in the allowlist ({', '.join(patterns) or 'empty'}): {', '.join(bad)}")


def build_message(args: SendEmailArgs, sender: str, key: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(args.to)
    msg["Subject"] = args.subject
    msg["Message-ID"] = f"<{key}@agentflow.local>"
    msg["X-AgentFlow-Idempotency-Key"] = key
    msg.set_content(args.body)
    return msg


@register_tool
class SendEmailTool(Tool):
    name = "send_email"
    description = ("Send a plain-text email. Always needs a human approval, who sees exactly what will be sent. "
                   "Only allowlisted recipients.")
    input_model = SendEmailArgs
    output_model = SendEmailResult
    side_effect = "external_write"
    supports_idempotency_key = True
    timeout_seconds = 30.0

    def replay_safe(self, options: dict[str, Any]) -> bool:
        return options.get("backend", "outbox") == "outbox"

    def available(self, options: dict[str, Any], settings) -> str | None:  # noqa: ANN001
        if options.get("backend", "outbox") == "smtp" and not options.get("smtp", {}).get("host"):
            return "smtp backend needs smtp.host"
        return None

    async def execute(self, args: SendEmailArgs, ctx: ToolContext) -> SendEmailResult:
        _check_recipients(args.to, list(ctx.options.get("allowed_recipients", [])),
                          bool(ctx.approval_flags.get("allow_recipient_override")))
        backend = ctx.options.get("backend", "outbox")
        smtp_cfg: dict[str, Any] = ctx.options.get("smtp", {})
        msg = build_message(args, smtp_cfg.get("from", "agentflow@localhost"), ctx.idempotency_key)
        if backend == "outbox":
            outbox = ctx.settings.outbox_dir
            outbox.mkdir(parents=True, exist_ok=True)
            target = outbox / f"{ctx.idempotency_key}.eml"
            data = msg.as_bytes()
            fd, tmp = tempfile.mkstemp(dir=outbox, prefix=".tmp-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
            return SendEmailResult(backend="outbox", message_id=msg["Message-ID"], recipients=args.to,
                                   subject=args.subject, location=str(target))
        if backend != "smtp":
            raise ToolError(f"unknown email backend {backend!r}")
        password = ctx.credentials.secret(smtp_cfg["credential"]) if ctx.credentials and smtp_cfg.get("credential") else None
        msg["Date"] = format_datetime(utcnow())

        def send() -> None:
            with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg.get("port", 587)), timeout=20) as server:
                if smtp_cfg.get("starttls", True):
                    server.starttls()
                if password:
                    server.login(smtp_cfg.get("username", smtp_cfg.get("from", "")), password)
                server.send_message(msg)

        try:
            await asyncio.to_thread(send)
        except (smtplib.SMTPException, OSError) as exc:
            raise ToolError(f"SMTP send failed: {type(exc).__name__}", transient=True) from exc
        return SendEmailResult(backend="smtp", message_id=msg["Message-ID"], recipients=args.to, subject=args.subject)
