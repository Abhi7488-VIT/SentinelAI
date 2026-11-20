"""Detection rules, state machine, explanations and signatures (pure; ADR 0006)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.detection.explain import explain
from app.detection.rules import (
    DEFAULTS,
    Cond,
    Evaluation,
    MemoryReading,
    PoolReading,
    Rule,
    error_rate,
    health_check,
    latency_shift,
    params_for,
    resource_saturation,
)
from app.detection.signatures import normalize_message, signature
from app.detection.state_machine import Snapshot, State, Step, step
from app.schemas.services import TelemetryConfig

ER = DEFAULTS[Rule.ERROR_RATE]
LAT = DEFAULTS[Rule.LATENCY_SHIFT]
SAT = DEFAULTS[Rule.RESOURCE_SATURATION]
T0 = datetime(2031, 1, 1, 12, 0, tzinfo=UTC)

# ---- rules: table-driven --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requests", "errors", "cond", "critical"),
    [
        (19, 19, Cond.NO_DATA, False),  # too few samples: never an anomaly
        (100, 4, Cond.NEUTRAL, False),  # 4%: between clear (2%) and fire (5%)
        (100, 5, Cond.FIRE, False),  # exactly at the fire threshold
        (100, 1, Cond.CLEAR, False),
        (100, 2, Cond.NEUTRAL, False),  # 2% is not below the clear threshold
        (100, 25, Cond.FIRE, True),  # critical threshold (severity still needs 3 min)
        (0, 0, Cond.NO_DATA, False),
    ],
)
def test_error_rate(requests: int, errors: int, cond: Cond, critical: bool) -> None:
    ev = error_rate(requests, errors, ER)
    assert ev.cond is cond
    assert ev.critical is critical


BASE = [20.0] * 12  # 12 baseline minutes of 20 ms p95


@pytest.mark.parametrize(
    ("p95", "samples", "baseline", "cond", "critical"),
    [
        (500.0, 60, [20.0] * 9, Cond.WARMING_UP, False),  # < 10 baseline minutes
        (500.0, 19, BASE, Cond.NO_DATA, False),
        (None, 0, BASE, Cond.NO_DATA, False),
        (60.0, 60, BASE, Cond.NEUTRAL, False),  # 3x but not +100 ms
        (120.0, 60, BASE, Cond.FIRE, False),  # max(3x, +100 ms) = 120 ms
        (119.0, 60, BASE, Cond.NEUTRAL, False),
        (29.0, 60, BASE, Cond.CLEAR, False),  # < 1.5x
        (200.0, 60, BASE, Cond.FIRE, True),  # >= 10x
    ],
)
def test_latency_shift(
    p95: float | None, samples: int, baseline: list[float], cond: Cond, critical: bool
) -> None:
    ev = latency_shift(p95, samples, baseline, LAT)
    assert ev.cond is cond
    assert ev.critical is critical


def test_latency_baseline_is_the_median() -> None:
    ev = latency_shift(1000.0, 60, [10.0] * 6 + [500.0] * 5, LAT)  # one-off slow minutes
    assert ev.baseline == 10.0


def _pool(used: float, pending: float, max_: float = 10) -> PoolReading:
    return PoolReading("i-1", "orders", used, max_, pending)


@pytest.mark.parametrize(
    ("pools", "memory", "cond", "metric"),
    [
        ([], [], Cond.NO_DATA, "pool_utilization"),
        ([_pool(10, 0)], [], Cond.NEUTRAL, "pool_utilization"),  # full, nobody waiting
        ([_pool(9, 1)], [], Cond.FIRE, "pool_utilization"),
        ([_pool(8, 1)], [], Cond.NEUTRAL, "pool_utilization"),
        ([_pool(6, 0)], [], Cond.CLEAR, "pool_utilization"),
        ([_pool(6, 0), _pool(10, 4)], [], Cond.FIRE, "pool_utilization"),  # worst instance
        ([], [MemoryReading("i-1", 86, 100)], Cond.FIRE, "memory_utilization"),
        ([], [MemoryReading("i-1", 69, 100)], Cond.CLEAR, "memory_utilization"),
        ([_pool(1, 0)], [MemoryReading("i-1", 90, 100)], Cond.FIRE, "memory_utilization"),
    ],
)
def test_resource_saturation(
    pools: list[PoolReading], memory: list[MemoryReading], cond: Cond, metric: str
) -> None:
    ev = resource_saturation(pools, memory, SAT)
    assert ev.cond is cond
    assert ev.metric == metric


@pytest.mark.parametrize(
    ("silent", "new5", "new2", "cond"),
    [
        (None, 0, 0, Cond.NO_DATA),  # never sent anything
        (900.0, 0, 0, Cond.NO_DATA),  # inactive > 10 min: not "lost", just quiet
        (5.0, 0, 0, Cond.CLEAR),
        (95.0, 0, 0, Cond.FIRE),  # heartbeat lost
        (5.0, 3, 1, Cond.FIRE),  # crash loop
        (5.0, 2, 1, Cond.NEUTRAL),  # a restart: not a loop, but not settled either
    ],
)
def test_health_check(silent: float | None, new5: int, new2: int, cond: Cond) -> None:
    assert health_check(silent, new5, new2, []).cond is cond


def test_overrides_apply_and_are_validated() -> None:
    p = params_for(Rule.ERROR_RATE, {"fire": 0.1, "min_samples": 50, "enabled": False})
    assert (p.fire, p.clear, p.min_samples, p.enabled) == (0.1, 0.02, 50, False)
    assert params_for(Rule.ERROR_RATE, None) == ER
    TelemetryConfig.model_validate({"detection": {"error_rate": {"fire": 0.1}}})
    with pytest.raises(ValidationError, match="clear must be below fire"):
        TelemetryConfig.model_validate({"detection": {"error_rate": {"fire": 0.01}}})
    with pytest.raises(ValidationError):
        TelemetryConfig.model_validate({"detection": {"deploy_change": {"fire": 2}}})
    with pytest.raises(ValidationError):
        TelemetryConfig.model_validate({"detection": {"error_rate": {"unknown": 1}}})


# ---- state machine --------------------------------------------------------------------------


def run(
    conds: list[Cond], *, min_s: int = 60, clear_s: int = 120, snap: Snapshot | None = None
) -> list[Step]:
    """Feed one condition per 15-s tick through the state machine."""
    snap = snap or Snapshot(State.OK, None, None)
    steps = []
    for i, cond in enumerate(conds):
        at = T0 + timedelta(seconds=15 * i)
        s = step(snap, cond, at, min_duration_s=min_s, clear_duration_s=clear_s)
        steps.append(s)
        snap = Snapshot(s.state, s.since, at)
    return steps


F, C, N, D, W = Cond.FIRE, Cond.CLEAR, Cond.NEUTRAL, Cond.NO_DATA, Cond.WARMING_UP


def fires(steps: list[Step]) -> int:
    return sum(s.fire for s in steps)


def test_fires_after_the_minimum_duration_with_the_pending_start() -> None:
    steps = run([F] * 6)
    assert [s.state for s in steps] == [State.PENDING] * 4 + [State.FIRING] * 2
    assert steps[4].fire
    assert steps[4].started_at == T0  # when the condition began, not when it fired


def test_short_spike_never_fires() -> None:
    assert fires(run([F, F, F, C, F, F, F, N, F, F, F, D] * 5)) == 0  # never 60 s in a row


def test_flapping_around_the_fire_threshold_produces_one_detection() -> None:
    # Fires, then the value hovers between clear and fire thresholds (NEUTRAL) with fire
    # spikes: hysteresis keeps it one detection, never resolved.
    steps = run([F] * 5 + [N, F, N, F, N, N, F, N] * 4)
    assert fires(steps) == 1
    assert not any(s.resolve for s in steps)


def test_resolves_only_after_clear_holds_for_the_clear_duration() -> None:
    steps = run([F] * 5 + [C] * 4 + [N] + [C] * 9)
    assert fires(steps) == 1
    resolves = [i for i, s in enumerate(steps) if s.resolve]
    # 4 CLEAR (45 s) then NEUTRAL resets to firing; then 9 CLEAR: resolves at the 9th (120 s).
    assert resolves == [len(steps) - 1]
    assert steps[-1].state is State.OK


def test_no_data_neither_fires_nor_resolves() -> None:
    reset = run([F, F, D] + [F] * 5)  # NO_DATA resets pending: the 60 s start again
    assert fires(reset) == 1
    assert reset[-1].fire
    assert reset[-1].started_at == T0 + timedelta(seconds=45)
    steps = run([F] * 5 + [D] * 20)
    assert steps[-1].state is State.FIRING
    assert not any(s.resolve for s in steps)


def test_warming_up_parks_idle_detectors_but_holds_firing_ones() -> None:
    assert run([W, W])[-1].state is State.WARMING_UP
    assert run([F] * 5 + [W] * 3)[-1].state is State.FIRING


def test_restart_mid_pending_resumes_from_the_persisted_state() -> None:
    first = run([F, F])  # pending since T0, then the worker dies
    persisted = Snapshot(first[-1].state, first[-1].since, T0 + timedelta(seconds=15))
    resumed = step(
        persisted, F, T0 + timedelta(seconds=30), min_duration_s=60, clear_duration_s=120
    )
    assert resumed.state is State.PENDING
    assert resumed.since == T0
    done = step(
        Snapshot(State.PENDING, T0, T0 + timedelta(seconds=45)),
        F,
        T0 + timedelta(seconds=60),
        min_duration_s=60,
        clear_duration_s=120,
    )
    assert done.fire
    assert done.started_at == T0


def test_unobserved_gap_restarts_the_pending_timer() -> None:
    snap = Snapshot(State.PENDING, T0, T0)
    later = T0 + timedelta(minutes=10)  # nobody evaluated for 10 minutes
    s = step(snap, F, later, min_duration_s=60, clear_duration_s=120)
    assert (s.state, s.since, s.fire) == (State.PENDING, later, False)


def test_zero_minimum_duration_fires_immediately() -> None:
    s = run([F], min_s=0)[0]
    assert s.fire
    assert s.started_at == T0


# ---- explanations and signatures ------------------------------------------------------------


def test_error_rate_explanation_snapshot() -> None:
    ev = error_rate(741, 412, ER)
    ev = Evaluation(
        ev.cond,
        ev.metric,
        ev.value,
        ev.baseline,
        ev.threshold,
        ev.critical,
        {**ev.facts, "baseline_ratio": 0.0},
    )
    text = explain(
        Rule.ERROR_RATE,
        "order-service",
        ev,
        window_end=datetime(2031, 1, 1, 19, 31, 45, tzinfo=UTC),
        started_at=datetime(2031, 1, 1, 19, 30, 30, tzinfo=UTC),
        min_duration_s=60,
        exceptions=[
            {"type": "TimeoutError", "message": "QueuePool limit of size # reached", "count": 388}
        ],
        sample_traces=["3f2a" + "0" * 28, "91c0" + "0" * 28],
    )
    assert text == (
        "order-service: error rate 55.6% (412 of 741 server requests) over 60 s ending "
        "19:31:45Z, above the 5.0% threshold for 75 s (fires after 60 s). Baseline over the "
        "previous 30 min: 0.0%. Top exception: `TimeoutError: QueuePool limit of size # "
        "reached` (388 spans). Sample traces: 3f2a0000…, 91c00000…."
    )


def test_signatures_ignore_variable_parts() -> None:
    a = "connection timed out after 30.00s (pool id 7f3a9c21d4, req 1234)"
    b = "connection timed out after 5.5s (pool id 00ab12cd99, req 99)"
    assert normalize_message(a) == "connection timed out after #s (pool id #, req #)"
    assert signature("TimeoutError", a) == signature("TimeoutError", b)
    assert signature("TimeoutError", a) != signature("ValueError", a)
