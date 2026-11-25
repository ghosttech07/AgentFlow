from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError

from app.config import Settings, get_settings

ZERO = "00000000-0000-0000-0000-000000000000"
CONTROL_CALLS = [
    ("post", f"/workflows/{ZERO}/pause", None),
    ("post", f"/workflows/{ZERO}/resume", None),
    ("post", f"/workflows/{ZERO}/cancel", None),
    ("post", f"/workflows/{ZERO}/retry", None),
    ("post", f"/approvals/{ZERO}/approve", {"actor": "a"}),
    ("post", f"/approvals/{ZERO}/reject", {"actor": "a", "reason": "r"}),
]


@pytest.fixture
def with_key(app: FastAPI) -> str:
    key = "s3cret-admin-key"
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"admin_api_key": SecretStr(key)})
    return key


@pytest.mark.parametrize(("method", "url", "body"), CONTROL_CALLS)
async def test_key_required_when_configured(client: AsyncClient, with_key: str, method: str, url: str,
                                            body: dict | None) -> None:
    for headers in ({}, {"X-API-Key": "wrong"}, {"X-API-Key": with_key + "x"}):
        resp = await client.request(method, url, json=body, headers=headers)
        assert resp.status_code == 401, (headers, resp.text)
        assert resp.json()["error"]["code"] == "unauthorized"
        assert with_key not in resp.text
    ok = await client.request(method, url, json=body, headers={"X-API-Key": with_key})
    assert ok.status_code == 404  # authorized; the (fake) id simply does not exist


@pytest.mark.parametrize(("method", "url", "body"), CONTROL_CALLS)
async def test_open_in_development_without_key(client: AsyncClient, method: str, url: str, body: dict | None) -> None:
    resp = await client.request(method, url, json=body)
    assert resp.status_code == 404


async def test_reads_stay_open_with_key(client: AsyncClient, with_key: str) -> None:
    assert (await client.get("/approvals")).status_code == 200
    assert (await client.get("/workflows")).status_code == 200


def test_production_requires_admin_key() -> None:
    with pytest.raises(ValidationError, match="ADMIN_API_KEY is required"):
        Settings(app_env="production", admin_api_key=None)
    assert Settings(app_env="production", admin_api_key="k").admin_api_key.get_secret_value() == "k"
