from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.llm.routes import RouteConfigError, load_routes


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def settings(path: Path, **extra: object):  # noqa: ANN201
    return get_settings().model_copy(update={"models_config_path": path, **extra})


def test_missing_file_builds_legacy_gemini_routes(tmp_path: Path) -> None:
    s = settings(tmp_path / "absent.yaml", gemini_model="gemini-3.5-flash",
                 gemini_fallback_models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"], gemini_worker_model=None)
    cfg = load_routes(s, available={"gemini"})
    assert cfg.route_for("planner") == ["gemini:gemini-3.5-flash", "gemini:gemini-3.5-flash-lite",
                                        "gemini:gemini-3.1-flash-lite"]
    assert cfg.route_for("writer") == cfg.route_for("planner")
    assert cfg.quotas == {}


def test_legacy_worker_model_goes_first_for_workers(tmp_path: Path) -> None:
    s = settings(tmp_path / "absent.yaml", gemini_model="gemini-3.5-flash",
                 gemini_fallback_models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
                 gemini_worker_model="gemini-3.5-flash-lite")
    cfg = load_routes(s, available={"gemini"})
    assert cfg.route_for("researcher") == ["gemini:gemini-3.5-flash-lite", "gemini:gemini-3.1-flash-lite",
                                           "gemini:gemini-3.5-flash"]
    assert cfg.route_for("ask") == cfg.route_for("researcher")
    assert cfg.route_for("planner")[0] == "gemini:gemini-3.5-flash"


def test_yaml_routes_per_role_and_quotas(tmp_path: Path) -> None:
    path = write(tmp_path, """
routes:
  default: ["ollama:qwen3:4b", "gemini:gemini-3.5-flash"]
  planner: ["gemini:gemini-3.5-flash"]
quotas:
  "gemini:gemini-3.5-flash": {requests_per_day: 20, timezone: America/Los_Angeles}
""")
    cfg = load_routes(settings(path), available={"gemini", "ollama"})
    assert cfg.route_for("planner") == ["gemini:gemini-3.5-flash"]
    assert cfg.route_for("writer") == ["ollama:qwen3:4b", "gemini:gemini-3.5-flash"]  # model names may hold ':'
    q = cfg.quotas["gemini:gemini-3.5-flash"]
    assert (q.requests_per_day, q.requests_per_minute, q.timezone) == (20, None, "America/Los_Angeles")


@pytest.mark.parametrize(("text", "fragment"), [
    ("routes: [this is: not valid", "Invalid YAML"),
    ("routes:\n  planner: ['gemini:x']\n", "default"),
    ("routes:\n  default: ['mystery:model-1']\n", "unknown provider 'mystery'"),
    ("routes:\n  default: ['no-colon-here']\n", "provider:model"),
    ("routes:\n  default: []\n", "empty"),
    ("routes:\n  default: ['gemini:x']\nquotas:\n  'gemini:x': {requests_per_day: -1}\n", "requests_per_day"),
    ("routes:\n  default: ['gemini:x']\nquotas:\n  'gemini:x': {timezone: Mars/Olympus}\n", "timezone"),
])
def test_invalid_config_gives_clear_error(tmp_path: Path, text: str, fragment: str) -> None:
    with pytest.raises(RouteConfigError) as exc:
        load_routes(settings(write(tmp_path, text)), available={"gemini"})
    assert fragment in str(exc.value) and "models.yaml" in str(exc.value)


def test_unconfigured_provider_skipped_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = write(tmp_path, "routes:\n  default: ['anthropic:claude-x', 'gemini:gemini-3.5-flash']\n")
    with caplog.at_level("WARNING"):
        cfg = load_routes(settings(path), available={"gemini"})
    assert cfg.route_for("default") == ["gemini:gemini-3.5-flash"]
    assert any("anthropic:claude-x" in r.getMessage() and "not configured" in r.getMessage() for r in caplog.records)


def test_role_without_usable_entry_fails_at_startup(tmp_path: Path) -> None:
    path = write(tmp_path, "routes:\n  default: ['gemini:gemini-3.5-flash']\n  writer: ['openai:gpt-x']\n")
    with pytest.raises(RouteConfigError, match="No usable model for role 'writer'"):
        load_routes(settings(path), available={"gemini"})


def test_fake_mode_routes_everything_to_fake(tmp_path: Path) -> None:
    path = write(tmp_path, "routes:\n  default: ['gemini:gemini-3.5-flash']\n")
    cfg = load_routes(settings(path, llm_provider="fake"), available={"fake"})
    assert cfg.route_for("planner") == cfg.route_for("writer") == ["fake:fake-model"]
