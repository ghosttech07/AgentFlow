"""web_search: backend chain (tavily -> ddgs -> gemini_grounding | fake), transient errors, disabled reason."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr

from app.config import get_settings
from app.tools import web_search
from app.tools.base import ToolContext, ToolError
from app.tools.web_search import WebSearchArgs, WebSearchTool


def ctx(settings=None, **options: object) -> ToolContext:  # noqa: ANN001
    s = settings or get_settings()
    return ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=s.workspaces_dir,
                       idempotency_key="k", settings=s, options=options)


@pytest.fixture
def backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"tavily": [], "ddgs": [], "gemini_grounding": []}
    behaviour: dict[str, object] = {}

    def make(name: str):  # noqa: ANN202
        async def run(query: str, settings, max_results: int):  # noqa: ANN001
            calls[name].append(query)
            result = behaviour.get(name, [{"title": name, "url": f"https://{name}.example/1", "snippet": "s"}])
            if isinstance(result, Exception):
                raise result
            return result
        return run

    for name in calls:
        monkeypatch.setitem(web_search.BACKENDS, name, make(name))
    calls["_behaviour"] = behaviour  # type: ignore[assignment]
    return calls


async def test_tavily_skipped_without_key_then_ddgs(backends) -> None:  # noqa: ANN001
    s = get_settings().model_copy(update={"tavily_api_key": None})
    out = await WebSearchTool().execute(WebSearchArgs(query="india ev sales"), ctx(s, backends=["tavily", "ddgs"]))
    assert out.backend == "ddgs" and out.results[0].url == "https://ddgs.example/1"
    assert backends["tavily"] == []


async def test_tavily_first_when_key_present(backends) -> None:  # noqa: ANN001
    s = get_settings().model_copy(update={"tavily_api_key": SecretStr("tvly-x")})
    out = await WebSearchTool().execute(WebSearchArgs(query="q"), ctx(s, backends=["tavily", "ddgs"]))
    assert out.backend == "tavily" and backends["ddgs"] == []


async def test_failing_or_empty_backend_falls_through(backends) -> None:  # noqa: ANN001
    # Both keys set explicitly: the chain must not depend on what the machine (or CI) has configured.
    s = get_settings().model_copy(update={"tavily_api_key": SecretStr("tvly-x"), "gemini_api_key": SecretStr("g-x")})
    backends["_behaviour"]["tavily"] = RuntimeError("429 rate limited")
    backends["_behaviour"]["ddgs"] = []  # "No results found" (throttled)
    out = await WebSearchTool().execute(WebSearchArgs(query="q"),
                                        ctx(s, backends=["tavily", "ddgs", "gemini_grounding"]))
    assert out.backend == "gemini_grounding"


async def test_all_backends_fail_is_a_transient_tool_error(backends) -> None:  # noqa: ANN001
    backends["_behaviour"]["ddgs"] = RuntimeError("blocked")
    with pytest.raises(ToolError) as exc:
        await WebSearchTool().execute(WebSearchArgs(query="q"), ctx(backends=["ddgs"]))
    assert exc.value.transient and "ddgs" in exc.value.message


async def test_fake_backend_and_malicious_query() -> None:
    normal = await WebSearchTool().execute(WebSearchArgs(query="india ev"), ctx(backends=["fake"]))
    evil = await WebSearchTool().execute(WebSearchArgs(query="malicious ev page"), ctx(backends=["fake"]))
    assert all("evil" not in h.url for h in normal.results)
    assert evil.results[0].url == "https://evil.example/ev-report"


def test_disabled_when_no_backend_is_usable() -> None:
    s = get_settings().model_copy(update={"tavily_api_key": None})
    assert "no usable" in WebSearchTool().available({"backends": ["tavily"]}, s)
    assert WebSearchTool().available({"backends": ["tavily", "ddgs"]}, s) is None
    assert WebSearchTool().untrusted and WebSearchTool.side_effect == "external_read"
