"""`detection_ref`: the incident's Phase 3 detections and their structural grouping reasons."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from app.investigation.evidence import EvidenceDraft
from app.investigation.reduce import cap, iso, rnd
from app.investigation.tools.base import ToolContext, ToolInputError, fmt_t
from app.models.investigation import Evidence
from app.repositories.incidents import IncidentDetectionRepository, IncidentRepository

MAX_DETECTIONS = 50


def _reasons(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": cap(str(r.get("type")), 40), "detail": cap(str(r.get("detail", "")), 200)}
        for r in raw[:5]
        if isinstance(r, dict)
    ]


async def detection_ref(ctx: ToolContext, *, incident_id: uuid.UUID) -> Evidence:
    incident = await IncidentRepository(ctx.session, ctx.scope).get(incident_id)
    if incident is None:
        raise ToolInputError("incident not found")
    await ctx.bound()
    members = (await IncidentDetectionRepository(ctx.session, ctx.scope).members([incident_id]))[
        incident_id
    ]
    names = await ctx.names_if_known([d.service_id for d, _ in members])
    detections = [
        {
            "detection_id": str(d.id),
            "service": names.get(d.service_id, "unknown"),
            "rule": d.rule,
            "severity": d.severity,
            "metric": d.metric,
            "observed": rnd(d.observed_value),
            "baseline": rnd(d.baseline_value),
            "threshold": rnd(d.threshold),
            "peak": rnd(d.peak_value),
            "started_at": iso(d.started_at),
            "fired_at": iso(d.fired_at),
            "resolved_at": iso(d.resolved_at),
            "explanation": cap(d.explanation, 300),
            "grouping_reasons": _reasons(link.reasons),
        }
        for d, link in members[:MAX_DETECTIONS]
    ]
    starts = [d.started_at for d, _ in members] or [incident.started_at]
    ends = [d.resolved_at or incident.last_activity_at for d, _ in members] or [
        incident.last_activity_at
    ]
    time_from = min([*starts, incident.started_at])
    time_to = max([*ends, incident.last_activity_at])
    if time_to <= time_from:
        time_to = time_from + timedelta(seconds=1)
    if members:
        first, _ = members[0]
        services = sorted({names.get(d.service_id, "unknown") for d, _ in members})
        summary = (
            f"Incident #{incident.number} ({incident.severity}, {incident.status}): "
            f"{len(members)} detections on {', '.join(services)}. First: {first.rule} on "
            f"{names.get(first.service_id, 'unknown')} started {fmt_t(first.started_at)}: "
            f"{cap(first.explanation, 300)}"
        )
    else:
        summary = f"Incident #{incident.number} has no linked detections."
    return await ctx.record(
        EvidenceDraft(
            kind="detection_ref",
            params={"incident_id": str(incident_id)},
            service_ids=sorted({d.service_id for d, _ in members}, key=str),
            time_from=time_from,
            time_to=time_to,
            summary=summary,
            data={
                "incident": {
                    "number": incident.number,
                    "title": cap(incident.title, 300),
                    "severity": incident.severity,
                    "status": incident.status,
                    "started_at": iso(incident.started_at),
                    "detected_at": iso(incident.detected_at),
                },
                "detections": detections,
            },
            row_count=len(members),
            truncated=len(members) > MAX_DETECTIONS,
        )
    )
