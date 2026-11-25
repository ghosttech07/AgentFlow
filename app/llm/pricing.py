"""Per-call cost from config/pricing.yaml (USD per 1M input/output tokens per "provider:model").

Never guesses: an unknown model costs None (logged once). Local and fake providers are free.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

FREE_PROVIDERS = frozenset({"ollama", "fake"})


class Pricing:
    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self.prices = prices
        self._warned: set[str] = set()

    @classmethod
    def load(cls, path: Path) -> Pricing:
        if not path.is_file():
            logger.warning("pricing.file_missing path=%s (all paid calls will have cost=null)", path)
            return cls({})
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        prices = {str(entry): (float(p["input"]), float(p["output"])) for entry, p in (raw.get("prices") or {}).items()}
        return cls(prices)

    def cost(self, entry: str, input_tokens: int, output_tokens: int) -> float | None:
        if entry.split(":", 1)[0] in FREE_PROVIDERS:
            return 0.0
        price = self.prices.get(entry)
        if price is None:
            if entry not in self._warned:
                self._warned.add(entry)
                logger.warning("pricing.unknown entry=%s cost recorded as null (add it to pricing.yaml)", entry)
            return None
        return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 8)
