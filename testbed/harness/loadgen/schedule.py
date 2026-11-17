"""Deterministic event schedules.

Each stream is an infinite generator of events at offsets (seconds) from the moment the mix
started. The generators depend only on (seed, mix name, stream), never on wall-clock time, so
the same seed always produces the same sequence of actions, users and targets.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass

from harness.loadgen.mixes import ACTIONS, Mix, Stream

CATEGORIES = ("audio", "books", "garden", "kitchen", "outdoor", "toys")
SKUS = tuple(f"SKU-{c[:3].upper()}-{n:03d}" for c in CATEGORIES for n in range(1, 51))
TOTAL_USERS = 5_000
VIRTUAL_USERS = 200


@dataclass(frozen=True)
class Event:
    at: float  # seconds after the mix started
    action: str  # a user action, or a stream kind
    user: int = 0  # shop user id (user actions only)
    target: str = ""  # category, sku, nonce, ...
    qty: int = 1


def virtual_users(seed: int) -> tuple[int, ...]:
    """The fixed set of shop accounts the simulated users sign in as."""
    rng = random.Random(f"{seed}:virtual-users")  # noqa: S311  (reproducible, not crypto)
    return tuple(sorted(rng.sample(range(1, TOTAL_USERS + 1), VIRTUAL_USERS)))


def user_events(seed: int, mix: Mix) -> Iterator[Event]:
    rng = random.Random(f"{seed}:{mix.name}:users")  # noqa: S311
    users = virtual_users(seed)
    actions = list(mix.weights)
    weights = [mix.weights[a] for a in actions]
    if not set(actions) <= set(ACTIONS):
        raise ValueError(f"mix {mix.name!r} has unknown actions")
    t = 0.0
    while True:
        t += rng.expovariate(mix.rps)
        action = rng.choices(actions, weights)[0]
        user = users[rng.randrange(len(users))]
        if action == "browse":
            target = "home" if rng.random() < 0.3 else rng.choice(CATEGORIES)
        elif action in ("product", "checkout"):
            target = rng.choice(SKUS)
        else:
            target = ""
        qty = rng.randint(1, 3) if action == "checkout" else 1
        yield Event(at=t, action=action, user=user, target=target, qty=qty)


def stream_events(seed: int, mix: Mix, stream: Stream) -> Iterator[Event]:
    """Fixed-rate streams (evenly spaced, so their pressure is steady and predictable)."""
    rng = random.Random(f"{seed}:{mix.name}:{stream.kind}")  # noqa: S311
    interval = 1.0 / stream.rps
    n = 0
    while True:
        n += 1
        if stream.kind == "catalogue_nonce":
            target = f"{rng.choice(CATEGORIES)}:{rng.getrandbits(64):016x}"
        else:
            target = stream.params.get("days", "7")
        yield Event(at=n * interval, action=stream.kind, target=target)
