"""Read side of detections and detector states (API). Tenant scoping via repositories."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.tenancy import TenantContext
from app.models.detection import Detection, DetectorState
from app.repositories.detection import DetectionRepository, DetectorStateRepository


class DetectionQueryService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.detections = DetectionRepository(session, ctx)
        self.states = DetectorStateRepository(session, ctx)

    async def list_page(
        self,
        *,
        limit: int,
        cursor: str | None,
        service_id: uuid.UUID | None,
        rule: str | None,
        status: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[list[Detection], str | None]:
        return await self.detections.list_filtered(
            limit=limit,
            cursor=cursor,
            service_id=service_id,
            rule=rule,
            status=status,
            start=start,
            end=end,
        )

    async def get(self, detection_id: uuid.UUID) -> Detection:
        det = await self.detections.get(detection_id)
        if det is None:
            raise NotFoundError("Detection not found.", code="detection_not_found")
        return det

    async def states_page(
        self, *, limit: int, cursor: str | None, service_id: uuid.UUID | None
    ) -> tuple[list[DetectorState], str | None]:
        return await self.states.list_filtered(limit=limit, cursor=cursor, service_id=service_id)
