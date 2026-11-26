"""send_email (outbox/smtp) + webhook_post; and every Part 9 tool is registered."""

from __future__ import annotations

import email
import uuid
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.tools import netguard
from app.tools.base import ToolContext, ToolError
from app.tools.email import SendEmailArgs, SendEmailTool
from app.tools.registry import all_tools
from app.tools.webhook import WebhookArgs, WebhookTool


def ctx(tmp_path: Path, key: str = "key-1", flags: dict | None = None, **options: object) -> ToolContext:
    s = get_settings().model_copy(update={"outbox_dir": tmp_path / "outbox"})
    return ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=tmp_path, idempotency_key=key,
                       settings=s, options={"backend": "outbox", "allowed_recipients": ["*@example.com"], **options},
                       approval_flags=flags or {})


def test_all_part9_tools_are_registered() -> None:
    import app.tools.catalog  # noqa: F401

    assert {"web_search", "web_fetch", "http_request", "sql_query", "calculator", "file_write", "file_read",
            "file_list", "send_email", "webhook_post"} <= set(all_tools())


async def test_outbox_writes_one_eml(tmp_path: Path) -> None:
    out = await SendEmailTool().execute(
        SendEmailArgs(to=["team@example.com"], subject="EV summary", body="Hello team"), ctx(tmp_path))
    files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 1 and out.backend == "outbox" and out.recipients == ["team@example.com"]
    msg = email.message_from_bytes(files[0].read_bytes())
    assert (msg["To"], msg["Subject"]) == ("team@example.com", "EV summary")
    assert msg["Message-ID"] == "<key-1@agentflow.local>" and "Hello team" in msg.get_payload()


async def test_same_idempotency_key_twice_is_one_file(tmp_path: Path) -> None:
    for _ in range(2):
        await SendEmailTool().execute(SendEmailArgs(to=["a@example.com"], subject="s", body="b"), ctx(tmp_path))
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1


async def test_recipient_outside_allowlist_refused_unless_approver_overrides(tmp_path: Path) -> None:
    args = SendEmailArgs(to=["attacker@evil.com"], subject="s", body="b")
    with pytest.raises(ToolError, match="allowlist"):
        await SendEmailTool().execute(args, ctx(tmp_path))
    out = await SendEmailTool().execute(args, ctx(tmp_path, flags={"allow_recipient_override": True}))
    assert out.recipients == ["attacker@evil.com"]


def test_override_is_not_an_llm_argument() -> None:
    assert "allow_recipient_override" not in SendEmailArgs.model_json_schema()["properties"]


def test_smtp_is_not_replay_safe_outbox_is() -> None:
    tool = SendEmailTool()
    assert tool.replay_safe({"backend": "outbox"}) and not tool.replay_safe({"backend": "smtp"})
    assert (tool.side_effect, SendEmailTool.untrusted) == ("external_write", False)


async def test_webhook_sends_idempotency_header_and_respects_allowlist(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["54.1.2.3"]

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    monkeypatch.setattr(netguard, "transport_factory",
                        lambda: httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, text="ok")))
    c = ctx(tmp_path, key="wh-1", allowed_urls=["https://hooks.slack.com/services/"])
    out = await WebhookTool().execute(WebhookArgs(url="https://hooks.slack.com/services/T1/B2", payload={"text": "hi"}), c)
    assert out.status == 200 and sent[0].headers["idempotency-key"] == "wh-1"
    assert sent[0].method == "POST" and b'"text"' in sent[0].content
    with pytest.raises(ToolError, match="allowlist"):
        await WebhookTool().execute(WebhookArgs(url="https://evil.example/hook", payload={}), c)
    assert WebhookTool().replay_safe({}) and WebhookTool.side_effect == "external_write"
