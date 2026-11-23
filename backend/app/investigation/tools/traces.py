"""`trace_analysis`: per-operation error rate and p95 before vs during (inbound SERVER and
database CLIENT operations, both cross-service by nature), top exception
signatures (type + normalised message + top frame), and critical paths of the slowest and
erroring traces with self-time per span."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from app.investigation.evidence import EvidenceDraft
from app.investigation.reduce import (
    SpanNode,
    cap,
    critical_path,
    normalize_message,
    pct_change,
    rnd,
    top_frame,
)
from app.investigation.tools.base import (
    ToolContext,
    ToolInputError,
    baseline_for,
    fmt_w,
    window,
    wparams,
)
from app.models.investigation import Evidence
from app.models.telemetry import SpanKind, SpanStatus
from app.repositories.investigation_queries import (
    LogEvidenceQueries,
    TraceEvidenceQueries,
    Window,
)

MAX_OPERATIONS = 30
MAX_SIGNATURES = 10
SLOWEST, ERRORING = 3, 2
TRACE_SPAN_LIMIT = 300
TRACE_MARGIN = timedelta(minutes=5)


def _rate(err: int, n: int) -> float | None:
    return None if not n else round(err / n * 100.0, 2)


def _operation(r: dict[str, Any], names: dict[uuid.UUID, str]) -> dict[str, Any]:
    b_rate, d_rate = _rate(r["b_err"], r["b_n"]), _rate(r["d_err"], r["d_n"])
    return {
        "service": names.get(r["service_id"], str(r["service_id"])),
        "operation": cap(r["operation"], 120),
        "kind": SpanKind(r["kind"]).name.lower(),
        "baseline": {
            "spans": r["b_n"],
            "errors": r["b_err"],
            "error_pct": b_rate,
            "p95_ms": rnd(r["b_p95"], 1),
        },
        "incident": {
            "spans": r["d_n"],
            "errors": r["d_err"],
            "error_pct": d_rate,
            "p95_ms": rnd(r["d_p95"], 1),
        },
        "error_pct_change_pp": None
        if b_rate is None or d_rate is None
        else round(d_rate - b_rate, 2),
        "p95_change_pct": pct_change(r["b_p95"], r["d_p95"]),
    }


def _signatures(
    span_rows: list[dict[str, Any]], log_rows: list[dict[str, Any]], names: dict[uuid.UUID, str]
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for source, rows in (("spans", span_rows), ("logs", log_rows)):
        for r in rows:
            svc = names.get(r["service_id"], str(r["service_id"]))
            key = (
                svc,
                (r["exception_type"] or "").strip()[:256],
                normalize_message(r["exception_message"]),
            )
            sig = merged.setdefault(
                key,
                {
                    "service": svc,
                    "type": cap(key[1], 120) or None,
                    "message": key[2] or None,
                    "top_frame": None,
                    "span_count": 0,
                    "log_count": 0,
                },
            )
            sig[f"{source[:-1]}_count"] += int(r["n"])
            if source == "logs" and sig["top_frame"] is None:
                sig["top_frame"] = top_frame(r.get("stacktrace"))
    out = sorted(
        merged.values(),
        key=lambda s: (
            -(s["span_count"] + s["log_count"]),
            s["service"],
            s["type"] or "",
            s["message"] or "",
        ),
    )
    return out[:MAX_SIGNATURES]


async def trace_analysis(
    ctx: ToolContext,
    *,
    service_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    baseline_start: datetime | None = None,
    baseline_end: datetime | None = None,
) -> Evidence:
    w = window(start, end)
    if (baseline_start is None) != (baseline_end is None):
        raise ToolInputError("give both baseline_start and baseline_end, or neither")
    given = Window(baseline_start, baseline_end) if baseline_start and baseline_end else None
    b = baseline_for(w, given)
    names = await ctx.service_names(service_ids)
    ids = sorted(names, key=str)
    await ctx.bound()
    q = TraceEvidenceQueries(ctx.session, ctx.scope)
    ops_rows = await q.operations(ids, b, w, MAX_OPERATIONS + 1)
    operations = [_operation(r, names) for r in ops_rows[:MAX_OPERATIONS]]

    signatures = _signatures(
        await q.span_exceptions(ids, w, 200),
        await LogEvidenceQueries(ctx.session, ctx.scope).exception_signatures(ids, w, 100),
        names,
    )

    paths: list[dict[str, Any]] = []
    for trace_id, reason in await q.sample_traces(ids, w, slowest=SLOWEST, erroring=ERRORING):
        spans = await q.trace_spans(
            trace_id, w.start - TRACE_MARGIN, w.end + TRACE_MARGIN, TRACE_SPAN_LIMIT
        )
        all_names = names | await ctx.names_if_known(
            [s.service_id for s in spans if s.service_id not in names]
        )
        nodes = [
            SpanNode(
                span_id=s.span_id.hex(),
                parent_span_id=s.parent_span_id.hex() if s.parent_span_id else None,
                service=all_names.get(s.service_id, "unknown"),
                name=s.name,
                start=s.start_time,
                end=s.end_time,
                duration_ms=s.duration_ms,
                error=s.status_code == int(SpanStatus.ERROR),
            )
            for s in spans
        ]
        paths.append(
            {
                "trace_id": trace_id.hex(),
                "reason": reason,
                "spans": len(spans),
                "truncated": len(spans) >= TRACE_SPAN_LIMIT,
                "path": critical_path(nodes),
            }
        )

    svc_list = ", ".join(sorted(names.values()))
    d_n = sum(o["incident"]["spans"] for o in operations)
    d_err = sum(o["incident"]["errors"] for o in operations)
    b_n = sum(o["baseline"]["spans"] for o in operations)
    b_err = sum(o["baseline"]["errors"] for o in operations)
    if d_n == 0 and b_n == 0:
        summary = f"No spans for {svc_list} in baseline {fmt_w(b)} or incident window {fmt_w(w)}."
    else:
        summary = (
            f"Spans of {svc_list}: incident window {fmt_w(w)} {d_n} spans, {d_err} errors "
            f"({_rate(d_err, d_n) or 0}%); baseline {b_n} spans, {b_err} errors "
            f"({_rate(b_err, b_n) or 0}%)."
        )
        if signatures:
            s = signatures[0]
            summary += (
                f" Top exception on {s['service']}: {s['type'] or 'error'}: {s['message'] or '-'}"
                f" (x{s['span_count'] + s['log_count']})."
            )
        slow = next((p for p in paths if p["reason"] == "slowest" and p["path"]), None)
        if slow:
            worst = max(slow["path"], key=lambda e: e["self_ms"] or 0)
            chain = " -> ".join(f"{e['service']}:{e['operation']}" for e in slow["path"][:6])
            summary += (
                f" Slowest critical path ({slow['path'][0]['duration_ms']} ms): {chain};"
                f" most self-time in {worst['service']}:{worst['operation']}"
                f" ({worst['self_ms']} ms)."
            )
    return await ctx.record(
        EvidenceDraft(
            kind="trace_analysis",
            params={
                "service_ids": [str(i) for i in ids],
                **wparams(w),
                "baseline": wparams(b),
            },
            service_ids=ids,
            time_from=b.start,
            time_to=w.end,
            summary=summary,
            data={
                "services": sorted(names.values()),
                "baseline": wparams(b),
                "window": wparams(w),
                "operations": operations,
                "exception_signatures": signatures,
                "critical_paths": paths,
            },
            row_count=d_n + b_n,
            truncated=len(ops_rows) > MAX_OPERATIONS,
        )
    )
