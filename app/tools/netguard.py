"""SSRF guard for every outbound HTTP request a tool makes (web_fetch, http_request, webhook_post).

Rules: http/https only; resolve DNS ourselves and require EVERY address to be public (a name with one
public and one private address is blocked); connect to the vetted IP (Host header + TLS SNI keep the
name), so there is no second DNS lookup an attacker could flip between check and connect; follow
redirects manually and re-check each hop; stream with a byte cap; content-type allowlist; timeout.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.tools.base import ToolError

USER_AGENT = "AgentFlow/2.0 (+self-hosted research agent; https://github.com/agentflow)"
DEFAULT_TYPES = ("text/html", "text/plain", "application/json", "application/xhtml+xml")
REDIRECTS = {301, 302, 303, 307, 308}


@dataclass
class FetchedResponse:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str]


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback
            or ip.is_link_local or ip.is_private or ip.is_unspecified)


async def _system_resolve(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"DNS lookup failed for {host}: {exc}", transient=True) from exc
    return sorted({info[4][0] for info in infos})


resolve = _system_resolve  # tests replace this


def transport_factory() -> httpx.AsyncBaseTransport:  # tests replace this
    return httpx.AsyncHTTPTransport(retries=0)


async def vet(url: str) -> tuple[httpx.URL, str]:
    """Check one URL; return the URL rewritten to the vetted IP and the original Host header value."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError(f"only http and https URLs are allowed (got {parts.scheme or 'none'!r})")
    host = parts.hostname
    if not host:
        raise ToolError("URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = [str(ipaddress.ip_address(host))]  # an IP literal
    except ValueError:
        addresses = await resolve(host, port)
    if not addresses:
        raise ToolError(f"DNS lookup returned no address for {host}", transient=True)
    for addr in addresses:
        if _blocked(ipaddress.ip_address(addr.split("%", 1)[0])):
            raise ToolError(f"blocked: {host} resolves to a non-public address ({addr})")
    target = httpx.URL(url).copy_with(host=addresses[0])
    host_header = host if parts.port is None else f"{host}:{parts.port}"
    if ":" in host and parts.port is None:  # IPv6 literal
        host_header = f"[{host}]"
    return target, host_header


async def guarded_request(
    url: str,
    *,
    method: str = "GET",
    max_bytes: int = 2_000_000,
    timeout: float = 20.0,
    allowed_types: tuple[str, ...] | None = DEFAULT_TYPES,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    max_redirects: int = 5,
) -> FetchedResponse:
    """One request with the guard on every hop. Returns the final response (body capped at max_bytes)."""
    current, method = url, method.upper()
    async with httpx.AsyncClient(transport=transport_factory(), timeout=timeout, follow_redirects=False) as client:
        for hop in range(max_redirects + 1):
            target, host_header = await vet(current)
            sni = urlsplit(current).hostname or ""
            request = client.build_request(
                method, target, params=params if hop == 0 else None,
                json=json_body if method not in ("GET", "HEAD") else None,
                headers={"User-Agent": USER_AGENT, **(headers or {}), "Host": host_header},
                extensions={"sni_hostname": sni} if urlsplit(current).scheme == "https" else None,
            )
            try:
                resp = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise ToolError(f"request to {sni} timed out after {timeout:g}s", transient=True) from exc
            except httpx.HTTPError as exc:
                raise ToolError(f"request to {sni} failed: {type(exc).__name__}", transient=True) from exc
            try:
                if resp.status_code in REDIRECTS and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    if resp.status_code == 303:
                        method = "GET"
                    continue
                ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if allowed_types is not None and resp.status_code < 400 and ctype not in allowed_types:
                    raise ToolError(f"content type {ctype or 'unknown'!r} is not allowed "
                                    f"(allowed: {', '.join(allowed_types)})")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise ToolError(f"response too large ({declared} bytes > {max_bytes})")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ToolError(f"response too large (> {max_bytes} bytes)")
                return FetchedResponse(url=url, final_url=current, status=resp.status_code, content_type=ctype,
                                       body=bytes(body), headers=dict(resp.headers))
            finally:
                await resp.aclose()
    raise ToolError(f"too many redirects (more than {max_redirects})")
