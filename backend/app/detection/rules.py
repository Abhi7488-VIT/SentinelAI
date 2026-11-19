"""Detection rules (Phase 3 T7, ADR 0006): pure functions from pre-aggregated numbers to a
condition. No I/O, no clock, no model: deterministic and table-testable.

Each rule returns an `Evaluation`:

- `FIRE`      the fire condition holds (enough samples, beyond the fire threshold);
- `CLEAR`     below the clear threshold (hysteresis: clear < fire);
- `NEUTRAL`   between the thresholds: a firing detection stays firing, a pending one resets;
- `NO_DATA`   too few samples to judge: never an anomaly, never a resolution;
- `WARMING_UP` the rule has no baseline yet (latency): shown in the UI, never fires.

Thresholds are the Phase 3 plan §6 defaults, set before any scenario was run; services may
override them in `telemetry_config.detection` (bounded, validated).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Any


class Rule(enum.StrEnum):
    ERROR_RATE = "error_rate"
    LATENCY_SHIFT = "latency_shift"
    RESOURCE_SATURATION = "resource_saturation"
    HEALTH_CHECK = "health_check"
    DEPLOY_CHANGE = "deploy_change"


class Cond(enum.StrEnum):
    FIRE = "fire"
    CLEAR = "clear"
    NEUTRAL = "neutral"
    NO_DATA = "no_data"
    WARMING_UP = "warming_up"


class Severity(enum.StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


SEVERITY_RANK = {Severity.WARNING: 1, Severity.CRITICAL: 2}

# A detection becomes critical only if beyond its critical threshold AND firing this long.
CRITICAL_AFTER_S = 180


@dataclass(frozen=True, slots=True)
class RuleParams:
    fire: float
    clear: float
    critical: float | None
    min_samples: int
    min_duration_s: int
    clear_duration_s: int
    enabled: bool = True


DEFAULTS: dict[Rule, RuleParams] = {
    # ratio of server spans with status ERROR, trailing 60 s
    Rule.ERROR_RATE: RuleParams(0.05, 0.02, 0.25, 20, 60, 120),
    # factor of trailing-60 s p95 over the baseline
    Rule.LATENCY_SHIFT: RuleParams(3.0, 1.5, 10.0, 20, 120, 120),
    # pool used/max (fire also needs pending > 0); memory uses MEMORY_* below
    Rule.RESOURCE_SATURATION: RuleParams(0.9, 0.7, None, 1, 60, 120),
    # thresholds unused (conditions are fixed facts: heartbeat lost / crash loop)
    Rule.HEALTH_CHECK: RuleParams(1.0, 0.0, None, 1, 0, 120),
    Rule.DEPLOY_CHANGE: RuleParams(1.0, 0.0, None, 1, 0, 0),
}

LATENCY_MIN_DELTA_MS = 100.0
LATENCY_BASELINE_MINUTES = 30
LATENCY_MIN_BASELINE_MINUTES = 10
MEMORY_FIRE = 0.85
MEMORY_CLEAR = 0.70
HEARTBEAT_LOST_S = 90
HEARTBEAT_ACTIVE_WINDOW_S = 600
CRASH_LOOP_INSTANCES = 3
CRASH_LOOP_WINDOW_S = 300
DEPLOY_WINDOW_S = 600


def params_for(rule: Rule, overrides: Mapping[str, Any] | None) -> RuleParams:
    """Defaults with a service's validated overrides (`telemetry_config.detection[rule]`)."""
    base = DEFAULTS[rule]
    if not overrides:
        return base
    allowed = {"fire", "clear", "critical", "min_samples", "min_duration_s", "clear_duration_s"}
    changes = {k: v for k, v in overrides.items() if k in allowed and v is not None}
    if "enabled" in overrides and overrides["enabled"] is not None:
        changes["enabled"] = bool(overrides["enabled"])
    return replace(base, **changes)


@dataclass(frozen=True, slots=True)
class Evaluation:
    cond: Cond
    metric: str
    value: float | None = None
    baseline: float | None = None
    threshold: float | None = None
    critical: bool = False
    # Numbers the explanation and the evidence use (all JSON-serializable).
    facts: dict[str, Any] = field(default_factory=dict)


# ---- error_rate -----------------------------------------------------------------------------


def error_rate(requests: int, errors: int, p: RuleParams) -> Evaluation:
    facts = {"requests": requests, "errors": errors, "window_s": 60}
    if requests < p.min_samples:
        return Evaluation(Cond.NO_DATA, "error_ratio", threshold=p.fire, facts=facts)
    ratio = errors / requests
    if ratio >= p.fire:
        cond = Cond.FIRE
    elif ratio < p.clear:
        cond = Cond.CLEAR
    else:
        cond = Cond.NEUTRAL
    critical = p.critical is not None and ratio >= p.critical
    return Evaluation(cond, "error_ratio", ratio, None, p.fire, critical, facts)


# ---- latency_shift --------------------------------------------------------------------------


def latency_baseline(minute_p95s: Sequence[float]) -> float | None:
    """Median of per-minute p95 values (minutes when this rule fired are excluded upstream)."""
    if len(minute_p95s) < LATENCY_MIN_BASELINE_MINUTES:
        return None
    return float(median(minute_p95s))


def latency_shift(
    p95_ms: float | None, samples: int, baseline_minutes: Sequence[float], p: RuleParams
) -> Evaluation:
    baseline = latency_baseline(baseline_minutes)
    facts: dict[str, Any] = {
        "samples": samples,
        "window_s": 60,
        "baseline_minutes": len(baseline_minutes),
        "p95_ms": p95_ms,
    }
    if baseline is None:
        return Evaluation(Cond.WARMING_UP, "p95_ms", p95_ms, None, None, facts=facts)
    threshold = max(baseline * p.fire, baseline + LATENCY_MIN_DELTA_MS)
    if p95_ms is None or samples < p.min_samples:
        return Evaluation(Cond.NO_DATA, "p95_ms", p95_ms, baseline, threshold, facts=facts)
    if p95_ms >= threshold:
        cond = Cond.FIRE
    elif p95_ms < baseline * p.clear:
        cond = Cond.CLEAR
    else:
        cond = Cond.NEUTRAL
    critical = p.critical is not None and p95_ms >= baseline * p.critical
    return Evaluation(cond, "p95_ms", p95_ms, baseline, threshold, critical, facts)


# ---- resource_saturation --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolReading:
    instance: str
    pool: str
    used: float
    max: float
    pending: float

    @property
    def ratio(self) -> float:
        return self.used / self.max if self.max > 0 else 0.0


@dataclass(frozen=True, slots=True)
class MemoryReading:
    instance: str
    used_bytes: float
    limit_bytes: int

    @property
    def ratio(self) -> float:
        return self.used_bytes / self.limit_bytes


def resource_saturation(
    pools: Sequence[PoolReading], memory: Sequence[MemoryReading], p: RuleParams
) -> Evaluation:
    """Fires on a saturated pool with waiters, or on memory near the declared limit."""
    if not pools and not memory:
        return Evaluation(Cond.NO_DATA, "pool_utilization", threshold=p.fire)
    worst_pool = max(pools, key=lambda r: (r.ratio, r.pending), default=None)
    worst_mem = max(memory, key=lambda r: r.ratio, default=None)
    facts: dict[str, Any] = {
        "pools": [
            {
                "instance": r.instance,
                "pool": r.pool,
                "used": r.used,
                "max": r.max,
                "pending": r.pending,
            }
            for r in sorted(pools, key=lambda r: (-r.ratio, -r.pending, r.instance, r.pool))[:10]
        ],
        "memory": [
            {"instance": r.instance, "used_bytes": r.used_bytes, "limit_bytes": r.limit_bytes}
            for r in sorted(memory, key=lambda r: (-r.ratio, r.instance))[:10]
        ],
    }
    pool_fire = any(r.ratio >= p.fire and r.pending > 0 for r in pools)
    mem_fire = any(r.ratio >= MEMORY_FIRE for r in memory)
    pool_clear = all(r.ratio < p.clear and r.pending == 0 for r in pools)
    mem_clear = all(r.ratio < MEMORY_CLEAR for r in memory)

    # Report the signal that matters: memory only when it alone fires or no pool is reported.
    metric, value, threshold = "pool_utilization", None, p.fire
    if worst_pool is not None:
        value = worst_pool.ratio
    if worst_mem is not None and ((mem_fire and not pool_fire) or worst_pool is None):
        metric, value, threshold = "memory_utilization", worst_mem.ratio, MEMORY_FIRE
    if pool_fire or mem_fire:
        cond = Cond.FIRE
    elif pool_clear and mem_clear:
        cond = Cond.CLEAR
    else:
        cond = Cond.NEUTRAL
    # "pool pending sustained": waiters right now; the 3-minute firing age is checked by caller.
    critical = pool_fire and worst_pool is not None and worst_pool.pending > 0
    return Evaluation(cond, metric, value, None, threshold, critical, facts)


# ---- health_check (passive) -----------------------------------------------------------------


def health_check(
    seconds_since_last_telemetry: float | None,
    new_instances_5m: int,
    new_instances_2m: int,
    instance_ids: Sequence[str],
) -> Evaluation:
    """Heartbeat loss (silent ≥ 90 s after being active in the last 10 min) or crash loop
    (≥ 3 new `service.instance.id`s in 5 min). Clears when telemetry is back and no new
    instance appeared for 2 min (the caller's clear duration adds the 120 s hold)."""
    silent = seconds_since_last_telemetry
    facts: dict[str, Any] = {
        "seconds_since_last_telemetry": None if silent is None else round(silent, 1),
        "new_instances_5m": new_instances_5m,
        "instance_ids": list(instance_ids)[:10],
    }
    if silent is None or silent >= HEARTBEAT_ACTIVE_WINDOW_S:
        return Evaluation(Cond.NO_DATA, "heartbeat", facts=facts)
    heartbeat_lost = silent >= HEARTBEAT_LOST_S
    crash_loop = new_instances_5m >= CRASH_LOOP_INSTANCES
    facts["heartbeat_lost"] = heartbeat_lost
    facts["crash_loop"] = crash_loop
    if heartbeat_lost or crash_loop:
        metric = "heartbeat_silence_s" if heartbeat_lost else "new_instances_5m"
        value = silent if heartbeat_lost else float(new_instances_5m)
        threshold = float(HEARTBEAT_LOST_S if heartbeat_lost else CRASH_LOOP_INSTANCES)
        return Evaluation(Cond.FIRE, metric, value, None, threshold, True, facts)
    if new_instances_2m == 0:
        return Evaluation(Cond.CLEAR, "heartbeat", silent, facts=facts)
    return Evaluation(Cond.NEUTRAL, "heartbeat", silent, facts=facts)
