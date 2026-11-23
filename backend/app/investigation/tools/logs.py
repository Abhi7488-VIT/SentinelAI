"""`log_query`: error/warn clusters of one service in a window."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.investigation.evidence import EvidenceDraft
from app.investigation.reduce import LogSample, cluster_logs
from app.investigation.tools.base import ToolContext, ToolInputError, fmt_w, window, wparams
from app.models.investigation import Evidence
from app.repositories.investigation_queries import LogEvidenceQueries

WARN = 13  # OTLP SeverityNumber WARN
SAMPLE_LIMIT = 2000
CLUSTERS = 20


async def log_query(
    ctx: ToolContext,
    *,
    service_id: uuid.UUID,
    start: datetime,
    end: datetime,
    min_severity: int = WARN,
) -> Evidence:
    if not 1 <= min_severity <= 24:
        raise ToolInputError("min_severity must be 1-24")
    w = window(start, end)
    name = (await ctx.service_names([service_id]))[service_id]
    await ctx.bound()
    q = LogEvidenceQueries(ctx.session, ctx.scope)
    total = await q.count(service_id, w, min_severity)
    raw = await q.samples(service_id, w, min_severity, SAMPLE_LIMIT) if total else []
    samples = [
        LogSample(
            record_id=r["record_id"],
            ts=r["ts"],
            severity_number=r["severity_number"],
            severity_text=r["severity_text"],
            body=r["body"],
            exception_type=r["exception_type"],
            exception_message=r["exception_message"],
            trace_id=r["trace_id"],
            attributes=dict(r["attributes"] or {}),
        )
        for r in raw
    ]
    found = cluster_logs(samples, top=CLUSTERS + 1)
    clusters = found[:CLUSTERS]
    if total == 0:
        summary = f"No log records at severity >= {min_severity} for {name} in {fmt_w(w)}."
    else:
        top = clusters[0]
        summary = (
            f"{total} log records at severity >= {min_severity} for {name} in {fmt_w(w)}"
            f"{f' ({len(samples)} most recent clustered)' if len(samples) < total else ''}; "
            f"{len(clusters)} clusters. Largest: x{top['count']} '{top['template']}' "
            f"(first {top['first_seen']}, last {top['last_seen']})."
        )
    return await ctx.record(
        EvidenceDraft(
            kind="log_query",
            params={"service_id": str(service_id), **wparams(w), "min_severity": min_severity},
            service_ids=[service_id],
            time_from=w.start,
            time_to=w.end,
            summary=summary,
            data={
                "service": name,
                "min_severity": min_severity,
                "total": total,
                "clustered": len(samples),
                "clusters": clusters,
            },
            row_count=total,
            truncated=len(samples) < total or len(found) > CLUSTERS,
        )
    )
