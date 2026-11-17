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

MIXES: dict[str, Mix] = {
    "baseline": Mix(name="baseline", rps=10.0, weights=BASELINE_WEIGHTS),
}


def get_mix(name: str) -> Mix:
    try:
        return MIXES[name]
    except KeyError:
        raise KeyError(f"unknown traffic mix {name!r}; known: {', '.join(sorted(MIXES))}") from None
