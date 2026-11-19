"""Project overview route (thin, viewer role)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.tenancy import ViewerCtx
from app.models.service import HealthStatus
from app.schemas.common import ErrorResponse
from app.schemas.overview import (
    MinutePoint,
    OverviewIncident,
    OverviewOut,
    OverviewServiceOut,
    RuleCount,
    SeverityCounts,
)
from app.services.overview import OverviewService

router = APIRouter(prefix="/orgs/{org_id}/overview", tags=["overview"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 422)
}


@router.get("", response_model=OverviewOut, responses=_errors)
async def get_overview(
    ctx: ViewerCtx,
    session: SessionDep,
    project_id: Annotated[uuid.UUID, Query()],
) -> OverviewOut:
    """Open incidents by severity, latest incidents, detections in 24 h by rule, and per active
    service: health, last seen, ingest accepted/rejected (15 min), requests/errors per minute
    (60 min)."""
    view = await OverviewService(session, ctx).get(project_id)
    return OverviewOut(
        project_id=view.project.id,
        generated_at=view.generated_at,
        open_incidents=SeverityCounts(
            critical=view.open_incidents.get("critical", 0),
            warning=view.open_incidents.get("warning", 0),
        ),
        latest_incidents=[
            OverviewIncident.model_validate(i, from_attributes=True) for i in view.latest_incidents
        ],
        detections_24h=[
            RuleCount.model_validate({"rule": r, "count": n})
            for r, n in sorted(view.detections_24h.items())
        ],
        services=[
            OverviewServiceOut(
                id=s.service.id,
                name=s.service.name,
                environment=s.service.environment,
                health_status=HealthStatus(s.service.health_status),
                last_seen_at=s.service.last_seen_at,
                ingest_accepted_15m=s.accepted_15m,
                ingest_rejected_15m=s.rejected_15m,
                series=[
                    MinutePoint(minute=p.minute, requests=p.requests, errors=p.errors)
                    for p in s.series
                ],
            )
            for s in view.services
        ],
        services_truncated=view.services_truncated,
    )
