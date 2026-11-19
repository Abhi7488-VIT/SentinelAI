"""Incident routes (thin). Viewers read; engineers change status."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import RequestMetaDep, SessionDep
from app.api.pagination import PageDep
from app.api.tenancy import EngineerCtx, ViewerCtx
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.models.incident import Incident
from app.models.service import Service
from app.schemas.common import ErrorResponse, Page
from app.schemas.detections import DetectionOut
from app.schemas.incidents import (
    IncidentDetailOut,
    IncidentDetectionOut,
    IncidentEventOut,
    IncidentOut,
    IncidentStatus,
    ServiceRef,
    Severity,
    TransitionIn,
)
from app.services.incidents import IncidentService

router = APIRouter(prefix="/orgs/{org_id}/incidents", tags=["incidents"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)
}


def _out(inc: Incident, services: list[Service]) -> dict[str, Any]:
    fields = {c: getattr(inc, c) for c in IncidentOut.model_fields if c != "services"}
    return {**fields, "services": [ServiceRef.model_validate(s) for s in services]}


@router.get("", response_model=Page[IncidentOut], responses=_errors)
async def list_incidents(
    ctx: ViewerCtx,
    session: SessionDep,
    page: PageDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[IncidentStatus | None, Query()] = None,
    severity: Annotated[Severity | None, Query()] = None,
    service_id: Annotated[uuid.UUID | None, Query()] = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> Page[IncidentOut]:
    """Newest first. `from`/`to` filter on `started_at`; `q` matches the title or `#number`."""
    rows, services, next_cursor = await IncidentService(session, ctx).list_page(
        limit=page.limit,
        cursor=page.cursor,
        project_id=project_id,
        status=status,
        severity=severity,
        service_id=service_id,
        start=start,
        end=end,
        q=q,
    )
    return Page[IncidentOut](
        items=[IncidentOut.model_validate(_out(r, services.get(r.id, []))) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/{incident_id}", response_model=IncidentDetailOut, responses=_errors)
async def get_incident(
    incident_id: uuid.UUID, ctx: ViewerCtx, session: SessionDep
) -> IncidentDetailOut:
    detail = await IncidentService(session, ctx).detail(incident_id)
    return IncidentDetailOut.model_validate(
        {
            **_out(detail.incident, detail.services),
            "detections": [
                IncidentDetectionOut(
                    detection=DetectionOut.model_validate(det),
                    reasons=link.reasons,  # type: ignore[arg-type]  # validated by pydantic
                    score=link.score,
                    linked_at=link.created_at,
                )
                for det, link in detail.detections
            ],
        }
    )


@router.post("/{incident_id}/transitions", response_model=IncidentOut, responses=_errors)
async def transition_incident(
    incident_id: uuid.UUID,
    body: TransitionIn,
    ctx: EngineerCtx,
    session: SessionDep,
    meta: RequestMetaDep,
) -> IncidentOut:
    svc = IncidentService(session, ctx)
    inc = await svc.transition(incident_id, to=body.to, note=body.note, meta=meta)
    detail = await svc.detail(inc.id)
    return IncidentOut.model_validate(_out(inc, detail.services))


@router.get("/{incident_id}/events", response_model=Page[IncidentEventOut], responses=_errors)
async def list_incident_events(
    incident_id: uuid.UUID,
    ctx: ViewerCtx,
    session: SessionDep,
    after_id: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Page[IncidentEventOut]:
    """Timeline in id order. `next_cursor` is the last id: pass it as `after_id`."""
    rows, next_cursor = await IncidentService(session, ctx).events_after(
        incident_id, after_id=after_id, limit=limit
    )
    return Page[IncidentEventOut](
        items=[IncidentEventOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )
