"""Symptom checks: a query against Prometheus, Loki or Jaeger plus a condition.

PromQL and LogQL checks are evaluated as range queries with a fixed step (STEP_S), so a symptom
is *measured* over the whole window: how many points met the condition, whether it held for
`min_points` consecutive steps (sustained) or for `min_fraction` of all points, its peak and its
onset (seconds after the window started). Jaeger checks count matching traces in the window.

When several series come back, they are reduced to one with max() per timestamp: "any series
above" for > / >=, "every series below" for < / <=.

Relative thresholds (`relative_to_baseline`): threshold = factor x the median of the same query
over the run's own baseline window, never below `min_value`.
"""

from __future__ import annotations

import math
import operator
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

STEP_S = 15
ONSET_BUCKET_S = 15

Point = tuple[float, float]

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


@dataclass(frozen=True)
class Condition:
    op: str
    threshold: float
    relative_to_baseline: bool = False
    min_value: float | None = None
    min_points: int | None = None
    min_fraction: float | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Condition:
        if data["op"] not in OPS:
            raise ValueError(f"unknown op {data['op']!r}")
        return cls(
            op=data["op"],
            threshold=float(data["threshold"]),
            relative_to_baseline=bool(data.get("relative_to_baseline", False)),
            min_value=None if data.get("min_value") is None else float(data["min_value"]),
            min_points=data.get("min_points"),
            min_fraction=data.get("min_fraction"),
        )


@dataclass(frozen=True)
class Check:
    id: str
    signal: str
    backend: str
    description: str
    query: str | dict[str, Any]
    condition: Condition

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Check:
        return cls(
            id=data["id"],
            signal=data["signal"],
            backend=data["backend"],
            description=data["description"],
            query=data["query"],
            condition=Condition.from_json(data["condition"]),
        )


@dataclass
class Observation:
    id: str
    observed: bool
    threshold: float
    points: int
    points_met: int
    peak: float | None
    onset_s: float | None
    baseline: float | None = None
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observed": self.observed,
            "baseline": self.baseline,
            "threshold": self.threshold,
            "points": self.points,
            "points_met": self.points_met,
            "peak": self.peak,
            "onset_s": self.onset_s,
            "detail": self.detail,
            "evidence": self.evidence,
        }


# ---- pure evaluation (unit-tested) ---------------------------------------------------------


def reduce_max(series: Sequence[dict[str, Any]]) -> list[Point]:
    """Prometheus/Loki matrix -> one series: max value per timestamp, NaN/Inf dropped."""
    by_ts: dict[float, float] = {}
    for s in series:
        for ts, raw in s.get("values", []):
            value = float(raw)
            if math.isnan(value) or math.isinf(value):
                continue
            t = float(ts)
            by_ts[t] = max(by_ts.get(t, value), value)
    return sorted(by_ts.items())


def baseline_value(points: Sequence[Point]) -> float | None:
    return statistics.median(v for _, v in points) if points else None


def effective_threshold(cond: Condition, baseline: float | None) -> float:
    if not cond.relative_to_baseline:
        return cond.threshold
    floor = cond.min_value if cond.min_value is not None else 0.0
    if baseline is None:
        return floor
    return max(cond.threshold * baseline, floor)


def longest_run(flags: Sequence[bool]) -> int:
    best = cur = 0
    for flag in flags:
        cur = cur + 1 if flag else 0
        best = max(best, cur)
    return best


def evaluate_points(
    check_id: str, points: Sequence[Point], cond: Condition, threshold: float, t0: float
) -> Observation:
    compare = OPS[cond.op]
    flags = [compare(v, threshold) for _, v in points]
    met = sum(flags)
    if not points:
        observed = False
    elif cond.min_fraction is not None:
        observed = met / len(points) >= cond.min_fraction
    else:
        observed = longest_run(flags) >= (cond.min_points or 1)
    onset = next((ts - t0 for (ts, _), ok in zip(points, flags, strict=True) if ok), None)
    peak = max((v for _, v in points), default=None)
    rule = (
        f">= {cond.min_fraction:.0%} of points"
        if cond.min_fraction is not None
        else f">= {cond.min_points or 1} consecutive points"
    )
    detail = f"{met}/{len(points)} points {cond.op} {threshold:.4g} (needs {rule})"
    return Observation(
        id=check_id,
        observed=observed,
        threshold=threshold,
        points=len(points),
        points_met=met,
        peak=peak,
        onset_s=None if onset is None else round(onset, 1) + 0.0,  # + 0.0: no "-0.0"
        detail=detail,
    )


def evaluate_traces(
    check_id: str, traces: Sequence[dict[str, Any]], service: str, cond: Condition, t0: float
) -> Observation:
    """Jaeger: the value is the number of matching traces; onset is the earliest span of
    `service` in them."""
    count = len(traces)
    starts = [
        sp["startTime"] / 1e6
        for tr in traces
        for sp in tr.get("spans", [])
        if tr.get("processes", {}).get(sp.get("processID"), {}).get("serviceName") == service
    ]
    observed = OPS[cond.op](count, cond.threshold)
    onset = (min(starts) - t0) if (starts and observed) else None
    return Observation(
        id=check_id,
        observed=observed,
        threshold=cond.threshold,
        points=count,
        points_met=count if observed else 0,
        peak=float(count),
        onset_s=None if onset is None else round(max(onset, 0.0), 1),
        detail=f"{count} matching traces {cond.op} {cond.threshold:g}?",
    )


def magnitude_bucket(peak: float | None, threshold: float, op: str) -> int | None:
    """log2(peak / threshold) for upward symptoms, clamped to 0..4; None otherwise."""
    if peak is None or op not in (">", ">=") or threshold <= 0 or peak <= 0:
        return None
    return max(0, min(4, math.floor(math.log2(peak / threshold))))


def fingerprint(observations: Sequence[Observation], checks: Sequence[Check]) -> dict[str, Any]:
    ops = {c.id: c.condition.op for c in checks}
    # A trace check's onset is the earliest of the newest `limit` matching traces, not when the
    # symptom began (seen live: 273 s vs 15 s for the same fault), so it is left out.
    trace_checks = {c.id for c in checks if c.backend == "jaeger"}
    symptoms = [
        {
            "id": o.id,
            "onset_bucket": (
                None
                if o.onset_s is None or o.id in trace_checks
                else int(o.onset_s // ONSET_BUCKET_S)
            ),
            "magnitude_bucket": magnitude_bucket(o.peak, o.threshold, ops.get(o.id, ">")),
        }
        for o in observations
        if o.observed
    ]
    return {"symptoms": sorted(symptoms, key=lambda s: s["id"])}


def compare_fingerprints(
    a: dict[str, Any], b: dict[str, Any], *, onset_tolerance: int = 1, magnitude_tolerance: int = 1
) -> list[str]:
    """Differences between two fingerprints (empty = equal within the stated tolerance).

    Tolerances: onset ±1 bucket (15 s of scheduling jitter); magnitude ±1 log2 bucket. The
    magnitude tolerance was set to 1 after two same-seed S1 runs peaked at 31% and 41% frontend
    5xx (log2 ratios 2.64 and 3.03): ±0 fails on bucket-boundary noise, not on a different
    symptom pattern. A ≥2-bucket difference (a factor of 4 or more) still fails.
    """
    sa = {s["id"]: s for s in a["symptoms"]}
    sb = {s["id"]: s for s in b["symptoms"]}
    diffs: list[str] = []
    if set(sa) != set(sb):
        diffs.append(f"symptom sets differ: {sorted(set(sa) ^ set(sb))}")
    for sid in sorted(set(sa) & set(sb)):
        for key, tol in (
            ("onset_bucket", onset_tolerance),
            ("magnitude_bucket", magnitude_tolerance),
        ):
            x, y = sa[sid][key], sb[sid][key]
            if (x is None) != (y is None) or (x is not None and abs(x - y) > tol):
                diffs.append(f"{sid}: {key} {x} vs {y} (tolerance ±{tol})")
    return diffs
