"""Detection explanations: fixed templates filled only from stored numbers (no model, no
free-form text except exception type/message, which are quoted and truncated telemetry)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.detection.rules import Evaluation, Rule


def _ts(t: datetime) -> str:
    return t.strftime("%H:%M:%SZ")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _quote(text: str | None, limit: int = 120) -> str:
    clean = (text or "").replace("`", "'").replace("\n", " ")
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _tail(exceptions: Sequence[dict[str, Any]], traces: Sequence[str]) -> str:
    parts = []
    if exceptions:
        top = exceptions[0]
        label = _quote(f"{top.get('type') or 'error'}: {top.get('message') or ''}".rstrip(": "))
        parts.append(f"Top exception: `{label}` ({top.get('count', 0)} spans).")
    if traces:
        parts.append("Sample traces: " + ", ".join(t[:8] + "…" for t in traces[:3]) + ".")
    return (" " + " ".join(parts)) if parts else ""


def explain(
    rule: Rule,
    service: str,
    ev: Evaluation,
    *,
    window_end: datetime,
    started_at: datetime,
    min_duration_s: int,
    exceptions: Sequence[dict[str, Any]] = (),
    sample_traces: Sequence[str] = (),
) -> str:
    held = int((window_end - started_at).total_seconds())
    f = ev.facts
    if rule is Rule.ERROR_RATE:
        baseline = f.get("baseline_ratio")
        base = (
            f" Baseline over the previous 30 min: {_pct(baseline)}." if baseline is not None else ""
        )
        return (
            f"{service}: error rate {_pct(ev.value)} ({f['errors']} of {f['requests']} server "
            f"requests) over 60 s ending {_ts(window_end)}, above the {_pct(ev.threshold)} "
            f"threshold for {held} s (fires after {min_duration_s} s).{base}"
            + _tail(exceptions, sample_traces)
        )
    if rule is Rule.LATENCY_SHIFT:
        factor = (ev.value or 0) / ev.baseline if ev.baseline else 0.0
        return (
            f"{service}: p95 latency {ev.value:.0f} ms over 60 s ending {_ts(window_end)} "
            f"({f['samples']} server requests), {factor:.1f}x the baseline of "
            f"{ev.baseline:.0f} ms (median per-minute p95 over the previous 30 min, "
            f"{f['baseline_minutes']} minutes); threshold {ev.threshold:.0f} ms, held {held} s "
            f"(fires after {min_duration_s} s)." + _tail(exceptions, sample_traces)
        )
    if rule is Rule.RESOURCE_SATURATION:
        if ev.metric == "memory_utilization":
            m = f["memory"][0]
            return (
                f"{service}: memory {_pct(ev.value)} of the declared limit "
                f"({m['used_bytes'] / 2**20:.0f} MiB of {m['limit_bytes'] / 2**20:.0f} MiB, "
                f"instance {_quote(m['instance'], 40)}) at {_ts(window_end)}, at or above "
                f"{_pct(ev.threshold)} for {held} s (fires after {min_duration_s} s)."
            )
        p = f["pools"][0]
        return (
            f"{service}: connection pool `{_quote(p['pool'], 60)}` {p['used']:.0f}/{p['max']:.0f} "
            f"in use ({_pct(ev.value)}) with {p['pending']:.0f} pending requests at "
            f"{_ts(window_end)} (instance {_quote(p['instance'], 40)}), at or above "
            f"{_pct(ev.threshold)} with waiters for {held} s (fires after {min_duration_s} s)."
            + _tail(exceptions, sample_traces)
        )
    if rule is Rule.HEALTH_CHECK:
        if f.get("heartbeat_lost"):
            return (
                f"{service}: no telemetry for {f['seconds_since_last_telemetry']:.0f} s as of "
                f"{_ts(window_end)} after sending data in the previous 10 min (heartbeat lost "
                f"after 90 s)."
            )
        return (
            f"{service}: {f['new_instances_5m']} new service instances within 5 min as of "
            f"{_ts(window_end)} (crash loop: 3 or more). Instances: "
            + ", ".join(_quote(i, 40) for i in f.get("instance_ids", [])[:5])
            + "."
        )
    if rule is Rule.DEPLOY_CHANGE:
        return (
            f"{service}: deployment {_quote(f['deployment_id'], 64)} "
            f"({_quote(f.get('previous_version'), 32) or '?'} -> "
            f"{_quote(f.get('version'), 32) or '?'}) "
            f"at {_ts(started_at)}, followed within 10 min by: "
            + "; ".join(_quote(x, 120) for x in f.get("linked", [])[:5])
            + "."
        )
    raise ValueError(f"unknown rule {rule}")
