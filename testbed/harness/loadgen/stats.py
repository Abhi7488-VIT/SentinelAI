"""Request statistics: a rolling 10-second window plus running totals."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


def percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


@dataclass
class Window:
    requests: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)  # 5xx or transport failure
    declined: int = 0  # payment declined (402): an expected business outcome
    latencies_ms: list[float] = field(default_factory=list)

    def record(self, action: str, latency_ms: float, *, error: bool, declined: bool) -> None:
        self.requests[action] += 1
        if error:
            self.errors[action] += 1
        if declined:
            self.declined += 1
        if len(self.latencies_ms) < 50_000:
            self.latencies_ms.append(latency_ms)

    def summary(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        total = sum(self.requests.values())
        errors = sum(self.errors.values())
        return {
            "requests": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "declined": self.declined,
            "p50_ms": round(p, 1) if (p := percentile(lat, 0.5)) is not None else None,
            "p95_ms": round(p, 1) if (p := percentile(lat, 0.95)) is not None else None,
            "by_action": dict(self.requests),
            "errors_by_action": dict(self.errors),
        }


@dataclass
class Stats:
    window: Window = field(default_factory=Window)
    totals: Window = field(default_factory=Window)
    dropped: int = 0  # events skipped because too many requests were already in flight

    def record(self, action: str, latency_ms: float, *, error: bool, declined: bool) -> None:
        self.window.record(action, latency_ms, error=error, declined=declined)
        self.totals.record(action, latency_ms, error=error, declined=declined)

    def roll(self) -> dict[str, Any]:
        summary = self.window.summary()
        self.window = Window()
        return summary
