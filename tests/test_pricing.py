from __future__ import annotations

from pathlib import Path

import pytest

from app.llm.pricing import Pricing


@pytest.fixture
def pricing(tmp_path: Path) -> Pricing:
    path = tmp_path / "pricing.yaml"
    path.write_text("prices:\n  \"anthropic:claude-x\": {input: 2.0, output: 10.0}\n"
                    "  \"gemini:g-1\": {input: 0.5, output: 3}\n", encoding="utf-8")
    return Pricing.load(path)


def test_cost_is_per_million_tokens(pricing: Pricing) -> None:
    # 1,000 input tokens at $2/M + 500 output tokens at $10/M = 0.002 + 0.005
    assert pricing.cost("anthropic:claude-x", 1000, 500) == pytest.approx(0.007)
    assert pricing.cost("gemini:g-1", 2_000_000, 0) == pytest.approx(1.0)


def test_local_and_fake_are_free(pricing: Pricing) -> None:
    assert pricing.cost("ollama:qwen3:4b", 10_000, 10_000) == 0.0
    assert pricing.cost("fake:fake-model", 10, 10) == 0.0


def test_unknown_model_is_null_with_one_warning(pricing: Pricing, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        assert pricing.cost("openai:gpt-unknown", 100, 100) is None
        assert pricing.cost("openai:gpt-unknown", 100, 100) is None
    warnings = [r for r in caplog.records if "pricing.unknown" in r.getMessage()]
    assert len(warnings) == 1 and "openai:gpt-unknown" in warnings[0].getMessage()


def test_missing_file_means_no_prices(tmp_path: Path) -> None:
    p = Pricing.load(tmp_path / "nope.yaml")
    assert p.cost("gemini:g-1", 1, 1) is None and p.cost("ollama:x", 1, 1) == 0.0


def test_shipped_pricing_file_is_valid() -> None:
    p = Pricing.load(Path("config/pricing.yaml"))
    assert p.prices, "config/pricing.yaml should list at least one price"
    assert all(i >= 0 and o >= 0 for i, o in p.prices.values())
