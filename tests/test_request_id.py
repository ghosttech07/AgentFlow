"""Part 7 audit fix: every request gets an id (X-Request-ID, echoed or generated) that appears in its log lines."""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient

from app.core.logging import RequestIdFilter, request_id_var


async def test_request_id_is_generated_and_returned(client: AsyncClient) -> None:
    resp = await client.get("/health")
    rid = resp.headers.get("x-request-id")
    assert rid and len(rid) >= 8


async def test_incoming_request_id_is_kept(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert resp.headers["x-request-id"] == "trace-abc-123"


async def test_unsafe_request_id_is_replaced(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "bad id\nwith newline" + "x" * 200})
    assert resp.headers["x-request-id"] != "bad id\nwith newline" and "\n" not in resp.headers["x-request-id"]


def test_log_records_carry_the_request_id(caplog: pytest.LogCaptureFixture) -> None:
    token = request_id_var.set("rid-42")
    try:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
        assert RequestIdFilter().filter(record) and record.request_id == "rid-42"
    finally:
        request_id_var.reset(token)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"
