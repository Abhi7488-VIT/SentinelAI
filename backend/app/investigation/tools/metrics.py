"""`metric_query`: per-series deltas, baseline window vs incident window, plus onset.

Gauges and non-monotonic sums compare values (mean, p95, max) and report the first point of the
incident window outside the baseline band (|v - mean| > max(3 sd, 10 % of |mean|)). Monotonic
sums compare rates; histograms compare the mean observation (sum / count). Counter resets make a
rate undefined (None), never guessed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.investigation.evidence import EvidenceDraft
from app.investigation.reduce import cap, iso, pct_change, rnd
from app.investigation.tools.base import (
    ToolContext,
    ToolInputError,
    baseline_for,
    fmt_t,
    fmt_w,
    window,
    wparams,
)
from app.models.investigation import Evidence
from app.models.telemetry import MetricKind
from app.repositories.investigation_queries import MetricEvidenceQueries, Window

MAX_SERIES_QUERIED = 200
MAX_SERIES_KEPT = 40
MAX_NAMES = 20
DELTA = 1  # OTLP AggregationTemporality DELTA


def _rate(r: dict[str, Any], p: str) -> float | None:
    first, last, span, starts = r[f"{p}_first"], r[f"{p}_last"], r[f"{p}_span"], r[f"{p}_starts"]
    if first is None or last is None or not span or (starts or 0) > 1 or last < first:
        return None
    return float(last - first) / float(span)


def _hist_mean(r: dict[str, Any], p: str) -> float | None:
    if r["temporality"] == DELTA:
        total, count = r[f"{p}_hsum"], r[f"{p}_hcount"]
    else:
        if (r[f"{p}_starts"] or 0) > 1 or r[f"{p}_hls"] is None or r[f"{p}_hfs"] is None:
            return None
        total = r[f"{p}_hls"] - r[f"{p}_hfs"]
        count = (r[f"{p}_hlc"] or 0) - (r[f"{p}_hfc"] or 0)
    if not count or total is None:
        return None
    return float(total) / float(count)


def _series(r: dict[str, Any]) -> dict[str, Any]:
    attrs = {
        cap(str(k), 60): cap(str(v), 80)
        for k, v in sorted((r["attributes"] or {}).items())[:6]
        if isinstance(v, str | int | float | bool)
    }
    out: dict[str, Any] = {
        "name": cap(r["name"], 120),
        "unit": cap(r["unit"], 32),
        "attributes": attrs,
        "points": {"baseline": r["b_n"] or 0, "incident": r["d_n"] or 0},
    }
    if r["kind"] == int(MetricKind.HISTOGRAM):
        b, d = _hist_mean(r, "b"), _hist_mean(r, "d")
        out |= {"measure": "mean_observation", "baseline": rnd(b), "incident": rnd(d)}
        out["change_pct"] = pct_change(b, d)
    elif r["kind"] == int(MetricKind.SUM) and r["is_monotonic"]:
        b, d = _rate(r, "b"), _rate(r, "d")
        out |= {"measure": "rate_per_s", "baseline": rnd(b), "incident": rnd(d)}
        out["change_pct"] = pct_change(b, d)
    else:
        out |= {
            "measure": "value",
            "baseline": {"mean": rnd(r["b_mean"]), "p95": rnd(r["b_p95"]), "max": rnd(r["b_max"])},
            "incident": {"mean": rnd(r["d_mean"]), "p95": rnd(r["d_p95"]), "max": rnd(r["d_max"])},
            "change_pct": pct_change(r["b_mean"], r["d_mean"]),
            "p95_change_pct": pct_change(r["b_p95"], r["d_p95"]),
            "onset": iso(r["onset"]),
        }
    return out


def _label(s: dict[str, Any]) -> str:
    attrs = ",".join(f"{k}={v}" for k, v in list(s["attributes"].items())[:2])
    return f"{s['name']}{{{attrs}}}" if attrs else str(s["name"])


def _fmt(v: Any) -> str:
    if isinstance(v, dict):
        v = v.get("mean")
    return "n/a" if v is None else f"{v:g}"


async def metric_query(
    ctx: ToolContext,
    *,
    service_id: uuid.UUID,
    start: datetime,
    end: datetime,
    baseline_start: datetime | None = None,
    baseline_end: datetime | None = None,
    names: Sequence[str] | None = None,
) -> Evidence:
    w = window(start, end)
    if (baseline_start is None) != (baseline_end is None):
        raise ToolInputError("give both baseline_start and baseline_end, or neither")
    given = Window(baseline_start, baseline_end) if baseline_start and baseline_end else None
    b = baseline_for(w, given)
    metric_names = sorted({n[:256] for n in names}) if names else None
    if metric_names is not None and len(metric_names) > MAX_NAMES:
        raise ToolInputError(f"at most {MAX_NAMES} metric names")
    svc = (await ctx.service_names([service_id]))[service_id]
    await ctx.bound()
    q = MetricEvidenceQueries(ctx.session, ctx.scope)
    total = await q.series_count(service_id, metric_names)
    rows = await q.deltas(service_id, b, w, metric_names, MAX_SERIES_QUERIED)
    series = [_series(r) for r in rows]
    series.sort(
        key=lambda s: (s["change_pct"] is None, -abs(s["change_pct"] or 0), s["name"], _label(s))
    )
    kept = series[:MAX_SERIES_KEPT]
    if not kept:
        summary = (
            f"No metric points for {svc} in baseline {fmt_w(b)} or incident window {fmt_w(w)}."
        )
    else:
        changed = [s for s in kept if s["change_pct"] is not None][:3]
        parts = [
            f"{_label(s)} {s['measure']} {_fmt(s['baseline'])} -> {_fmt(s['incident'])} "
            f"({s['change_pct']:+g}%)"
            + (f", onset {fmt_t(datetime.fromisoformat(s['onset']))}" if s.get("onset") else "")
            for s in changed
        ]
        summary = f"{len(series)} metric series of {svc}, baseline {fmt_w(b)} vs {fmt_w(w)}. " + (
            "Largest changes: " + "; ".join(parts) if parts else "No comparable change."
        )
    return await ctx.record(
        EvidenceDraft(
            kind="metric_query",
            params={
                "service_id": str(service_id),
                **wparams(w),
                "baseline": wparams(b),
                "names": metric_names,
            },
            service_ids=[service_id],
            time_from=b.start,
            time_to=w.end,
            summary=summary,
            data={
                "service": svc,
                "baseline": wparams(b),
                "window": wparams(w),
                "series_total": total,
                "series": kept,
            },
            row_count=total,
            truncated=total > len(kept),
        )
    )
