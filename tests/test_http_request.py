"""http_request: domain allowlist, method-dependent side effect, named credentials, redaction."""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest

from app.config import get_settings
from app.tools import netguard
from app.tools.base import ToolContext, ToolError
from app.tools.config import CredentialResolver, ToolsConfig
from app.tools.http_request import HttpRequestArgs, HttpRequestTool
from app.tools.redact import redact

PUBLIC = "140.82.112.6"
SECRET = "ghp_TOPSECRET_123"


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    async def fake_resolve(host: str, port: int) -> list[str]:
        return [PUBLIC]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "echo": request.headers.get("authorization", "")[:6]})

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    monkeypatch.setattr(netguard, "transport_factory", lambda: httpx.MockTransport(handler))
    return sent


def ctx() -> ToolContext:
    cfg = ToolsConfig.model_validate({"credentials": {"github": {"env": "GH_T"}}})
    return ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=get_settings().workspaces_dir,
                       idempotency_key="k", settings=get_settings(),
                       options={"allowed_domains": ["api.github.com", "*.example.com"]},
                       credentials=CredentialResolver(cfg, {"GH_T": SECRET}))


async def test_domain_not_in_allowlist_is_refused(net) -> None:  # noqa: ANN001
    with pytest.raises(ToolError, match="allowlist"):
        await HttpRequestTool().execute(HttpRequestArgs(url="https://evil.example.org/x"), ctx())
    assert net == []


async def test_wildcard_domain_and_get(net) -> None:  # noqa: ANN001
    out = await HttpRequestTool().execute(HttpRequestArgs(url="https://api.example.com/v1/items"), ctx())
    assert out.status == 200 and '"ok"' in out.body_text


async def test_credential_injected_into_header_only(net, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN001
    with caplog.at_level(logging.DEBUG):
        out = await HttpRequestTool().execute(
            HttpRequestArgs(method="GET", url="https://api.github.com/user", credential="github"), ctx())
    assert net[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in out.model_dump_json()
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_side_effect_depends_on_method() -> None:
    tool = HttpRequestTool()
    assert tool.side_effect_for(HttpRequestArgs(url="https://api.github.com/x")) == "external_read"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert tool.side_effect_for(HttpRequestArgs(method=method, url="https://api.github.com/x")) == "external_write"


def test_redact_masks_secret_values_and_auth_headers() -> None:
    data = {"headers": {"Authorization": "Bearer abc", "X-Api-Key": "k1", "Accept": "json"},
            "body": f"token={SECRET}", "list": [SECRET, 1]}
    out = redact(data, [SECRET])
    assert out["headers"] == {"Authorization": "***", "X-Api-Key": "***", "Accept": "json"}
    assert out["body"] == "token=***" and out["list"] == ["***", 1]
