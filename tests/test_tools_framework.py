"""Part 9 tool framework: registry, tools.yaml validation, effective tools, approval defaults, GET /tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import BaseModel

from app.config import get_settings
from app.tools import registry
from app.tools.base import Tool, ToolContext
from app.tools.config import (
    CredentialResolver,
    ToolConfigError,
    approval_mode,
    effective_tools,
    get_tools_config,
    load_tools_config,
)


class EchoArgs(BaseModel):
    text: str


class EchoOut(BaseModel):
    text: str


class EchoTool(Tool):
    name = "echo_test"
    description = "Echo the text back."
    input_model = EchoArgs
    output_model = EchoOut
    side_effect = "none"

    async def execute(self, args: EchoArgs, ctx: ToolContext) -> EchoOut:
        return EchoOut(text=args.text)


class SendTest(EchoTool):
    name = "send_test"
    side_effect = "external_write"


@pytest.fixture(autouse=True)
def _test_tools():  # noqa: ANN202
    registry.register_tool(EchoTool)
    registry.register_tool(SendTest)
    yield
    registry.unregister_tool("echo_test")
    registry.unregister_tool("send_test")
    get_tools_config.cache_clear()


def cfg_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "tools.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def settings(path: Path):  # noqa: ANN201
    return get_settings().model_copy(update={"tools_config_path": path})


def test_registry_get_and_list() -> None:
    assert isinstance(registry.get_tool("echo_test"), EchoTool)
    assert {"echo_test", "send_test"} <= set(registry.all_tools())
    with pytest.raises(KeyError):
        registry.get_tool("nope")


def test_missing_config_means_no_tools(tmp_path: Path) -> None:
    cfg = load_tools_config(settings(tmp_path / "absent.yaml"))
    assert cfg is None
    assert effective_tools("researcher", ("echo_test",), cfg) == []


def test_effective_tools_is_intersection(tmp_path: Path) -> None:
    cfg = load_tools_config(settings(cfg_file(tmp_path, """
tools:
  echo_test: {}
  send_test: {enabled: false}
agents:
  writer: [echo_test, send_test]
""")))
    assert effective_tools("writer", ("echo_test", "send_test"), cfg) == ["echo_test"]  # disabled dropped
    assert effective_tools("writer", ("send_test",), cfg) == []  # class allowlist wins too
    assert effective_tools("analyst", ("echo_test",), cfg) == []  # config gives analyst nothing


@pytest.mark.parametrize(("text", "fragment"), [
    ("tools:\n  mystery_tool: {}\n", "unknown tool 'mystery_tool'"),
    ("tools:\n  echo_test: {}\nagents:\n  wizard: [echo_test]\n", "unknown agent 'wizard'"),
    ("tools:\n  echo_test: {}\nagents:\n  writer: [ghost]\n", "unknown tool 'ghost'"),
    ("tools:\n  echo_test: {approval: sometimes}\n", "approval"),
    ("tools: [not, a, mapping\n", "Invalid YAML"),
])
def test_invalid_config_errors(tmp_path: Path, text: str, fragment: str) -> None:
    with pytest.raises(ToolConfigError) as exc:
        load_tools_config(settings(cfg_file(tmp_path, text)))
    assert fragment in str(exc.value) and "tools.yaml" in str(exc.value)


def test_default_approval_for_external_write_is_always(tmp_path: Path) -> None:
    cfg = load_tools_config(settings(cfg_file(tmp_path, "tools:\n  echo_test: {}\n  send_test: {}\n")))
    assert approval_mode(registry.get_tool("send_test"), cfg) == "always"
    assert approval_mode(registry.get_tool("echo_test"), cfg) == "never"
    cfg2 = load_tools_config(settings(cfg_file(tmp_path, "tools:\n  send_test: {approval: never}\n")))
    assert approval_mode(registry.get_tool("send_test"), cfg2) == "never"  # taint still overrides (Task 12)


def test_credentials_resolve_from_env_and_never_leak(tmp_path: Path) -> None:
    cfg = load_tools_config(settings(cfg_file(tmp_path, """
tools: {}
credentials:
  github: {env: GH_TEST_TOKEN}
  custom: {env: CUSTOM_KEY, header: X-Api-Key, format: "{secret}"}
""")))
    res = CredentialResolver(cfg, {"GH_TEST_TOKEN": "ghp_secret123", "CUSTOM_KEY": "k-999"})
    assert res.headers("github") == {"Authorization": "Bearer ghp_secret123"}
    assert res.headers("custom") == {"X-Api-Key": "k-999"}
    assert set(res.secret_values()) == {"ghp_secret123", "k-999"}
    with pytest.raises(Exception, match="unknown credential"):
        res.headers("nope")


async def test_get_tools_endpoint_never_shows_secrets(client: AsyncClient, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TEST_TOKEN", "ghp_supersecret")
    path = cfg_file(tmp_path, """
tools:
  echo_test: {}
  send_test: {}
agents:
  writer: [echo_test]
credentials:
  github: {env: GH_TEST_TOKEN}
""")
    monkeypatch.setenv("TOOLS_CONFIG_PATH", str(path))
    get_settings.cache_clear()
    get_tools_config.cache_clear()
    try:
        resp = await client.get("/tools")
    finally:
        monkeypatch.delenv("TOOLS_CONFIG_PATH")
        get_settings.cache_clear()
    assert resp.status_code == 200
    assert "ghp_supersecret" not in resp.text
    tools = {t["name"]: t for t in resp.json()["tools"]}
    assert tools["echo_test"]["agents"] == ["writer"] and tools["echo_test"]["enabled"] is True
    assert tools["send_test"]["approval"] == "always" and tools["send_test"]["side_effect"] == "external_write"
    assert tools["echo_test"]["input_schema"]["properties"]["text"]["type"] == "string"
    assert resp.json()["configured"] is True
