"""web_fetch + the SSRF guard (app/tools/netguard.py). No real network: a fake resolver + httpx.MockTransport."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.config import get_settings
from app.tools import netguard
from app.tools.base import ToolContext, ToolError
from app.tools.web_fetch import WebFetchArgs, WebFetchTool

PUBLIC = "93.184.216.34"
HTML = (b"<html><head><title>EV report</title></head><body><article><h1>EV sales</h1>"
        b"<p>India sold 2.1 million electric two-wheelers in 2025, a record year for the segment. " * 3
        + b"</p></article></body></html>")


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {"example.com": [PUBLIC], "news.example.com": [PUBLIC]}

    async def fake_resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise ToolError(f"DNS lookup failed for {host}")
        return table[host]

    monkeypatch.setattr(netguard, "resolve", fake_resolve)
    return table


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Routes keyed by (Host header, path); records every request the guard actually sent."""
    routes: dict[tuple[str, str], httpx.Response] = {}
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return routes.get((request.headers["host"], request.url.path), httpx.Response(404, text="nope"))

    monkeypatch.setattr(netguard, "transport_factory", lambda: httpx.MockTransport(handler))
    return routes, sent


def ctx(**options: object) -> ToolContext:
    return ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=get_settings().workspaces_dir,
                       idempotency_key="k", settings=get_settings(), options={"backend": "http", **options})


async def fetch(url: str, **options: object):  # noqa: ANN201
    return await WebFetchTool().execute(WebFetchArgs(url=url), ctx(**options))


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[fc00::1]/", "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/", "http://224.0.0.1/",
])
async def test_private_and_special_addresses_are_blocked(url: str, dns, server) -> None:  # noqa: ANN001
    dns["localhost"] = ["127.0.0.1"]
    with pytest.raises(ToolError, match="blocked"):
        await fetch(url)
    assert server[1] == []  # nothing was sent


async def test_name_resolving_to_private_ip_is_blocked(dns, server) -> None:  # noqa: ANN001
    dns["internal.example.org"] = ["10.0.0.5"]
    with pytest.raises(ToolError, match="blocked"):
        await fetch("http://internal.example.org/")


async def test_name_with_one_public_and_one_private_address_is_blocked(dns, server) -> None:  # noqa: ANN001
    dns["mixed.example.org"] = [PUBLIC, "192.168.0.10"]  # Review Focus 5: any private address blocks
    with pytest.raises(ToolError, match="blocked"):
        await fetch("http://mixed.example.org/")


async def test_redirect_to_private_ip_is_blocked(dns, server) -> None:  # noqa: ANN001
    routes, sent = server
    routes[("example.com", "/r")] = httpx.Response(302, headers={"location": "http://169.254.169.254/x"})
    with pytest.raises(ToolError, match="blocked"):
        await fetch("http://example.com/r")
    assert len(sent) == 1  # the hop to the metadata address was never requested


async def test_too_many_redirects(dns, server) -> None:  # noqa: ANN001
    routes, _ = server
    for i in range(7):
        routes[("example.com", f"/{i}")] = httpx.Response(302, headers={"location": f"/{i + 1}"})
    with pytest.raises(ToolError, match="redirects"):
        await fetch("http://example.com/0")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"])
async def test_non_http_schemes_refused(url: str, dns, server) -> None:  # noqa: ANN001
    with pytest.raises(ToolError, match="http"):
        await fetch(url)


async def test_response_over_size_limit_is_aborted(dns, server) -> None:  # noqa: ANN001
    server[0][("example.com", "/big")] = httpx.Response(200, headers={"content-type": "text/html"},
                                                        content=b"x" * 5000)
    with pytest.raises(ToolError, match="too large"):
        await fetch("http://example.com/big", max_bytes=1000)


async def test_disallowed_content_type_refused(dns, server) -> None:  # noqa: ANN001
    server[0][("example.com", "/img")] = httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    with pytest.raises(ToolError, match="content type"):
        await fetch("http://example.com/img")


async def test_html_page_extracted_and_connection_pinned_to_vetted_ip(dns, server) -> None:  # noqa: ANN001
    routes, sent = server
    routes[("example.com", "/ev")] = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                                    content=HTML)
    out = await fetch("http://example.com/ev")
    assert out.title == "EV report" and "2.1 million" in out.text
    assert out.final_url == "http://example.com/ev" and out.content_type == "text/html"
    assert sent[0].url.host == PUBLIC  # connects to the checked address (no second DNS lookup)
    assert sent[0].headers["host"] == "example.com" and "AgentFlow" in sent[0].headers["user-agent"]


async def test_redirect_followed_to_public_page(dns, server) -> None:  # noqa: ANN001
    routes, _ = server
    routes[("example.com", "/old")] = httpx.Response(301, headers={"location": "http://news.example.com/ev"})
    routes[("news.example.com", "/ev")] = httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)
    out = await fetch("http://example.com/old")
    assert out.final_url == "http://news.example.com/ev"


async def test_bs4_fallback_when_trafilatura_finds_nothing(dns, server) -> None:  # noqa: ANN001
    server[0][("example.com", "/tiny")] = httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html><title>T</title><body><div>Short note</div></body></html>")
    out = await fetch("http://example.com/tiny")
    assert "Short note" in out.text


async def test_fake_backend_serves_canned_pages() -> None:
    out = await WebFetchTool().execute(WebFetchArgs(url="https://evil.example/ev-report"), ctx(backend="fake"))
    assert "ignore previous instructions" in out.text.lower()
    with pytest.raises(ToolError):
        await WebFetchTool().execute(WebFetchArgs(url="https://unknown.example/x"), ctx(backend="fake"))


def test_web_fetch_is_untrusted_external_read() -> None:
    assert (WebFetchTool.side_effect, WebFetchTool.untrusted) == ("external_read", True)
