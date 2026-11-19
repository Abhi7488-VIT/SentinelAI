"""Detections and detector states (thin, read-only, viewer role)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.pagination import PageDep
from app.api.tenancy import ViewerCtx
from app.schemas.common import ErrorResponse, Page
from app.schemas.detections import DetectionOut, DetectorStateOut, RuleName
from app.services.detections_query import DetectionQueryService

router = APIRouter(prefix="/orgs/{org_id}", tags=["detections"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 422)
}


@router.get("/detections", response_model=Page[DetectionOut], responses=_errors)
async def list_detections(
    ctx: ViewerCtx,
    session: SessionDep,
    page: PageDep,
    service_id: Annotated[uuid.UUID | None, Query()] = None,
    rule: Annotated[RuleName | None, Query()] = None,
    status: Annotated[Literal["open", "resolved"] | None, Query()] = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
) -> Page[DetectionOut]:
    """Newest first. `from`/`to` filter on `started_at` (half-open)."""
    rows, next_cursor = await DetectionQueryService(session, ctx).list_page(
        limit=page.limit,
        cursor=page.cursor,
        service_id=service_id,
        rule=rule,
        status=status,
        start=start,
        end=end,
    )
    return Page[DetectionOut](
        items=[DetectionOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )


@router.get("/detections/{detection_id}", response_model=DetectionOut, responses=_errors)
async def get_detection(
    detection_id: uuid.UUID, ctx: ViewerCtx, session: SessionDep
) -> DetectionOut:
    return DetectionOut.model_validate(await DetectionQueryService(session, ctx).get(detection_id))


@router.get("/detector-states", response_model=Page[DetectorStateOut], responses=_errors)
async def list_detector_states(
    ctx: ViewerCtx,
    session: SessionDep,
    page: PageDep,
    service_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[DetectorStateOut]:
    rows, next_cursor = await DetectionQueryService(session, ctx).states_page(
        limit=page.limit, cursor=page.cursor, service_id=service_id
    )
    return Page[DetectorStateOut](
        items=[DetectorStateOut.model_validate(r) for r in rows], next_cursor=next_cursor
    )
