"""`deploy_lookup`: deployments of the services in [start - 2 h, end] and their distance to
the onset."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.investigation.evidence import EvidenceDraft
from app.investigation.reduce import cap, iso
from app.investigation.tools.base import ToolContext, ToolInputError, fmt_t, fmt_w, window, wparams
from app.models.investigation import Evidence
from app.repositories.detection import DeploymentQueries
from app.repositories.investigation_queries import Window

LOOKBACK = timedelta(hours=2)
MAX_ROWS = 50


async def deploy_lookup(
    ctx: ToolContext,
    *,
    service_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    onset: datetime | None = None,
) -> Evidence:
    w = window(start, end)
    onset = onset or w.start
    if not w.start <= onset <= w.end:
        raise ToolInputError("onset must lie inside the window")
    names = await ctx.service_names(service_ids)
    ids = sorted(names, key=str)
    span = Window(w.start - LOOKBACK, w.end)
    await ctx.bound()
    found = await DeploymentQueries(ctx.session, ctx.scope).between(ids, span.start, span.end)
    rows = [
        {
            "service": names[d.service_id],
            "deployment_id": cap(d.deployment_id, 128),
            "status": cap(d.status, 32),
            "version": cap(d.version, 64),
            "previous_version": cap(d.previous_version, 64),
            "at": iso(d.ts),
            "minutes_before_onset": round((onset - d.ts).total_seconds() / 60, 1),
        }
        for d in found[:MAX_ROWS]
    ]
    svc_list = ", ".join(sorted(names.values()))
    if not rows:
        summary = f"No deployments of {svc_list} in {fmt_w(span)}."
    else:
        parts = [
            f"{r['service']} {r['previous_version'] or '?'} -> {r['version'] or '?'} at "
            f"{fmt_t(d.ts)} ({r['minutes_before_onset']} min before onset)"
            for r, d in zip(rows[:5], found[:5], strict=True)
        ]
        summary = f"{len(found)} deployments of {svc_list} in {fmt_w(span)}: " + "; ".join(parts)
    return await ctx.record(
        EvidenceDraft(
            kind="deploy_lookup",
            params={"service_ids": [str(i) for i in ids], **wparams(w), "onset": onset.isoformat()},
            service_ids=ids,
            time_from=span.start,
            time_to=span.end,
            summary=summary,
            data={"onset": iso(onset), "deployments": rows},
            row_count=len(found),
            truncated=len(found) > MAX_ROWS,
        )
    )
