"""`dependency_lookup`: declared and observed edges around the services, each with its own
traffic before vs during (calls, error rate, p95)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.investigation.evidence import EvidenceDraft
from app.investigation.reduce import pct_change, rnd
from app.investigation.tools.base import (
    ToolContext,
    ToolInputError,
    baseline_for,
    fmt_w,
    window,
    wparams,
)
from app.models.investigation import Evidence
from app.repositories.investigation_queries import EdgeEvidenceQueries, TraceEvidenceQueries, Window

MAX_EDGES = 50
MAX_EDGES_WITH_STATS = 20


def _side(n: int, err: int, p95: float | None) -> dict[str, Any]:
    return {
        "calls": n,
        "errors": err,
        "error_pct": None if not n else round(err / n * 100.0, 2),
        "p95_ms": rnd(p95, 1),
    }


def _stats(r: dict[str, Any]) -> dict[str, Any]:
    before = _side(r["b_n"], r["b_err"], r["b_p95"])
    during = _side(r["d_n"], r["d_err"], r["d_p95"])
    b_pct, d_pct = before["error_pct"], during["error_pct"]
    return {
        "baseline": before,
        "incident": during,
        "error_pct_change_pp": None if b_pct is None or d_pct is None else round(d_pct - b_pct, 2),
        "p95_change_pct": pct_change(r["b_p95"], r["d_p95"]),
        "calls_change_pct": pct_change(r["b_n"], r["d_n"]),
    }


def _magnitude(e: dict[str, Any]) -> float:
    s = e.get("stats")
    if not s:
        return -1.0
    return float(abs(s["error_pct_change_pp"] or 0) + abs(s["p95_change_pct"] or 0) / 10)


async def dependency_lookup(
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
    rows = await EdgeEvidenceQueries(ctx.session, ctx.scope).around(ids, MAX_EDGES + 1)
    other_ids = {r.source_service_id for r in rows} | {
        r.target_service_id for r in rows if r.target_service_id
    }
    all_names = names | await ctx.names_if_known([i for i in other_ids if i not in names])

    # One logical connection may have a declared and an observed row; merge them.
    edges: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    for r in rows[:MAX_EDGES]:
        target = str(r.target_service_id) if r.target_service_id else f"ext:{r.target_external}"
        e = edges.setdefault(
            (r.source_service_id, target),
            {
                "source": all_names.get(r.source_service_id, "unknown"),
                "target": all_names.get(r.target_service_id, "unknown")
                if r.target_service_id
                else r.target_external,
                "target_kind": "service" if r.target_service_id else "external",
                "sources": [],
                "_src": r.source_service_id,
                "_tgt": r.target_service_id,
                "_ext": r.target_external,
            },
        )
        e["sources"] = sorted({*e["sources"], r.source})

    q = TraceEvidenceQueries(ctx.session, ctx.scope)
    ordered = sorted(
        edges.values(), key=lambda e: (e["source"], e["target_kind"], str(e["target"]))
    )
    for e in ordered[:MAX_EDGES_WITH_STATS]:
        if e["_tgt"] is not None:
            e["stats"] = _stats(await q.edge_server_stats(e["_src"], e["_tgt"], b, w))
        elif e["_ext"] and ":" in e["_ext"]:
            system, target = e["_ext"].split(":", 1)
            e["stats"] = _stats(await q.edge_client_stats(e["_src"], system, target, b, w))
    out = [{k: v for k, v in e.items() if not k.startswith("_")} for e in ordered]
    out.sort(key=lambda e: (-_magnitude(e), e["source"], str(e["target"])))

    svc_list = ", ".join(sorted(names.values()))
    if not out:
        summary = f"No declared or observed dependency edges touch {svc_list}."
    else:
        parts = []
        for e in out[:3]:
            s = e.get("stats")
            if not s:
                parts.append(f"{e['source']} -> {e['target']} (no traffic stats)")
                continue
            parts.append(
                f"{e['source']} -> {e['target']}: errors {s['baseline']['error_pct'] or 0}% -> "
                f"{s['incident']['error_pct'] or 0}%, p95 {s['baseline']['p95_ms'] or 'n/a'} -> "
                f"{s['incident']['p95_ms'] or 'n/a'} ms, calls {s['baseline']['calls']} -> "
                f"{s['incident']['calls']}"
            )
        summary = (
            f"{len(out)} dependency edges around {svc_list}, baseline {fmt_w(b)} vs {fmt_w(w)}. "
            + "; ".join(parts)
            + "."
        )
    return await ctx.record(
        EvidenceDraft(
            kind="dependency_lookup",
            params={"service_ids": [str(i) for i in ids], **wparams(w), "baseline": wparams(b)},
            service_ids=ids,
            time_from=b.start,
            time_to=w.end,
            summary=summary,
            data={"services": sorted(names.values()), "edges": out},
            row_count=len(out),
            truncated=len(rows) > MAX_EDGES or len(ordered) > MAX_EDGES_WITH_STATS,
        )
    )
