"""In-memory per-model circuit breaker (per process; resets on restart, which is fine).

closed    -> calls allowed; consecutive overload failures are counted
open      -> after THRESHOLD consecutive failures: model skipped until COOLDOWN passes
half-open -> after the cooldown exactly one trial call is allowed; success closes the
             circuit, failure re-opens it for another cooldown
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass
class _State:
    failures: int = 0
    opened_at: float | None = None
    trial_in_flight: bool = False


class CircuitBreaker:
    def __init__(
        self, threshold: int, cooldown_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self._clock = clock
        self._states: dict[str, _State] = {}

    def _state(self, model: str) -> _State:
        return self._states.setdefault(model, _State())

    def state(self, model: str) -> str:
        s = self._state(model)
        if s.opened_at is None:
            return "closed"
        return "half_open" if self._clock() - s.opened_at >= self.cooldown else "open"

    def allow(self, model: str) -> bool:
        s = self._state(model)
        current = self.state(model)
        if current == "closed":
            return True
        if current == "half_open" and not s.trial_in_flight:
            s.trial_in_flight = True  # exactly one trial call
            return True
        return False

    def release(self, model: str) -> None:
        """Give back a half-open trial without an outcome (the call was cancelled)."""
        self._state(model).trial_in_flight = False

    def record_success(self, model: str) -> None:
        self._states[model] = _State()

    def record_failure(self, model: str) -> None:
        s = self._state(model)
        s.failures += 1
        if s.trial_in_flight or s.failures >= self.threshold:
            s.opened_at = self._clock()  # (re)open for a full cooldown
            s.trial_in_flight = False

    def seconds_until_available(self, models: Iterable[str]) -> float | None:
        """Shortest wait until any of the models may be tried again (None if one is usable)."""
        waits = []
        for m in models:
            s = self._state(m)
            if s.opened_at is None:
                return None
            waits.append(max(0.0, self.cooldown - (self._clock() - s.opened_at)))
        return min(waits) if waits else None
