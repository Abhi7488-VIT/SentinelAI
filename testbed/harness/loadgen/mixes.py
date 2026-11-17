"""Traffic mixes: what the simulated users (and internal jobs) do, and how often."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Action = Literal["browse", "product", "account", "login", "checkout"]
StreamKind = Literal["internal_report", "catalogue_nonce"]

ACTIONS: tuple[Action, ...] = ("browse", "product", "account", "login", "checkout")


@dataclass(frozen=True)
class Stream:
    """An extra, fixed-rate request stream layered on top of the user traffic."""

    kind: StreamKind
    rps: float
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Mix:
    name: str
    rps: float  # user actions per second (Poisson arrivals)
    weights: dict[Action, float]
    streams: tuple[Stream, ...] = ()


BASELINE_WEIGHTS: dict[Action, float] = {
    "browse": 50,
    "product": 20,
    "account": 15,
    "login": 5,
    "checkout": 10,
}

# Scenario mixes: baseline user traffic plus one extra fixed-rate stream. Stream rates are
# calibrated live (docs/testbed.md) so the fault produces its symptoms within the observe window.
WEEKLY_REPORT_RPS = 4.0  # S1: GET /internal/reports/sales?days=7 per second
CACHE_BUSTING_RPS = 45.0  # S5: unique product-list URLs per second
# (measured live: 20/s grew api-gateway 0.26 MiB/s, ~13 kB per URL -> ~8 min to its 192 MB limit;
# 45/s targets ~0.6 MiB/s, i.e. the limit in ~3-4 min as the plan requires)

MIXES: dict[str, Mix] = {
    "baseline": Mix(name="baseline", rps=10.0, weights=BASELINE_WEIGHTS),
    "with-weekly-reports": Mix(
        name="with-weekly-reports",
        rps=10.0,
        weights=BASELINE_WEIGHTS,
        streams=(Stream("internal_report", WEEKLY_REPORT_RPS, {"days": "7"}),),
    ),
    "cache-busting-client": Mix(
        name="cache-busting-client",
        rps=10.0,
        weights=BASELINE_WEIGHTS,
        streams=(Stream("catalogue_nonce", CACHE_BUSTING_RPS),),
    ),
}


def get_mix(name: str) -> Mix:
    try:
        return MIXES[name]
    except KeyError:
        raise KeyError(f"unknown traffic mix {name!r}; known: {', '.join(sorted(MIXES))}") from None
