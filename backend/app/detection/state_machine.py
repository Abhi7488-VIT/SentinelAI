"""The detector state machine shared by every rule (Phase 3 plan §6, ADR 0006). Pure.

    ok ──FIRE──▶ pending ──FIRE held min_duration──▶ FIRING (detection created,
     ▲             │ anything else                     │      started = pending since)
     │             │                                   │ CLEAR
     └─────────────┘                                   ▼
    ok ◀── CLEAR held clear_duration ── resolving ── FIRE / NEUTRAL ──▶ firing (same detection)

- Hysteresis: FIRE and CLEAR use different thresholds; NEUTRAL (between them) keeps a firing
  detection firing and resets a pending one, so flapping around the fire threshold yields one
  detection at most.
- NO_DATA never fires and never resolves: pending resets, firing/resolving hold.
- WARMING_UP (no baseline) parks ok/pending detectors in `warming_up`; firing ones hold.
- Gaps: if the previous evaluation is older than `max_gap_s` (worker down), a pending or
  resolving timer restarts now instead of counting time nobody observed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from app.detection.rules import Cond


class State(enum.StrEnum):
    OK = "ok"
    PENDING = "pending"
    FIRING = "firing"
    RESOLVING = "resolving"
    WARMING_UP = "warming_up"


MAX_GAP_S = 60.0


@dataclass(frozen=True, slots=True)
class Snapshot:
    state: State
    since: datetime | None
    last_evaluated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Step:
    state: State
    since: datetime
    fire: bool = False  # create a detection; it started at `started_at`
    resolve: bool = False  # resolve the open detection
    started_at: datetime | None = None


def step(
    snap: Snapshot,
    cond: Cond,
    at: datetime,
    *,
    min_duration_s: float,
    clear_duration_s: float,
    max_gap_s: float = MAX_GAP_S,
) -> Step:
    state = snap.state
    since = snap.since or at
    gap = (
        snap.last_evaluated_at is None or (at - snap.last_evaluated_at).total_seconds() > max_gap_s
    )
    if gap and state in (State.PENDING, State.RESOLVING):
        since = at  # restart timers across unobserved time

    if state in (State.FIRING, State.RESOLVING):
        return _active(state, since, cond, at, clear_duration_s)

    # ok / pending / warming_up
    if cond is Cond.WARMING_UP:
        return Step(State.WARMING_UP, since if state is State.WARMING_UP else at)
    if cond is not Cond.FIRE:
        return Step(State.OK, since if state is State.OK else at)
    if state is not State.PENDING:
        since = at
    if (at - since).total_seconds() >= min_duration_s:
        return Step(State.FIRING, at, fire=True, started_at=since)
    return Step(State.PENDING, since)


def _active(state: State, since: datetime, cond: Cond, at: datetime, clear_s: float) -> Step:
    if cond in (Cond.FIRE, Cond.NEUTRAL):
        return Step(State.FIRING, since if state is State.FIRING else at)
    if cond is Cond.CLEAR:
        start = since if state is State.RESOLVING else at
        if (at - start).total_seconds() >= clear_s:
            return Step(State.OK, at, resolve=True)
        return Step(State.RESOLVING, start)
    # NO_DATA / WARMING_UP: hold
    return Step(state, since)
