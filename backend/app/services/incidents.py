"""Incident use cases for the API: list, detail, status transitions, timeline.

Tenant isolation comes from the scoped repositories. Status changes are the only user writes;
everything else on an incident is derived by the correlator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import TenantContext
from app.models.detection import Detection
from app.models.incident import Incident, IncidentDetection, IncidentEvent
from app.models.service import Service
from app.repositories.incidents import (
    IncidentDetectionRepository,
    IncidentEventRepository,
    IncidentRepository,
    IncidentServiceRepository,
)
from app.repositories.services import ServiceRepository
from app.services.audit import AuditService, RequestMeta

# open → investigating → mitigated → resolved; open/investigating → resolved; resolved → open.
TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"investigating", "resolved"}),
    "investigating": frozenset({"mitigated", "resolved"}),
    "mitigated": frozenset({"resolved"}),
    "resolved": frozenset({"open"}),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True, slots=True)
class IncidentDetail:
    incident: Incident
    services: list[Service]
    detections: list[tuple[Detection, IncidentDetection]]


class IncidentService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.session = session
        self.ctx = ctx
        self.incidents = IncidentRepository(session, ctx)
        self.links = IncidentDetectionRepository(session, ctx)
        self.inc_services = IncidentServiceRepository(session, ctx)
        self.events = IncidentEventRepository(session, ctx)
        self.services = ServiceRepository(session, ctx)
        self.audit = AuditService(session)

    async def list_page(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: uuid.UUID | None,
        status: str | None,
        severity: str | None,
        service_id: uuid.UUID | None,
        start: datetime | None,
        end: datetime | None,
        q: str | None,
    ) -> tuple[list[Incident], dict[uuid.UUID, list[Service]], str | None]:
        rows, next_cursor = await self.incidents.list_filtered(
            limit=limit,
            cursor=cursor,
            project_id=project_id,
            status=status,
            severity=severity,
            service_id=service_id,
            start=start,
            end=end,
            q=q,
        )
        return rows, await self._services_of([r.id for r in rows]), next_cursor

    async def _services_of(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Service]]:
        by_incident = await self.inc_services.of(ids)
        all_ids = sorted({s for v in by_incident.values() for s in v})
        services = {s.id: s for s in await self.services.get_many(all_ids)}
        return {i: [services[s] for s in v if s in services] for i, v in by_incident.items()}

    async def get(self, incident_id: uuid.UUID) -> Incident:
        inc = await self.incidents.get(incident_id)
        if inc is None:
            raise NotFoundError("Incident not found.", code="incident_not_found")
        return inc

    async def detail(self, incident_id: uuid.UUID) -> IncidentDetail:
        inc = await self.get(incident_id)
        services = (await self._services_of([inc.id])).get(inc.id, [])
        detections = (await self.links.members([inc.id])).get(inc.id, [])
        return IncidentDetail(inc, services, detections)

    async def transition(
        self, incident_id: uuid.UUID, *, to: str, note: str | None, meta: RequestMeta
    ) -> Incident:
        inc = await self.incidents.get_for_update(incident_id)
        if inc is None:
            raise NotFoundError("Incident not found.", code="incident_not_found")
        if not can_transition(inc.status, to):
            raise ConflictError(
                f"Cannot change an incident from {inc.status} to {to}.",
                code="invalid_transition",
            )
        previous = inc.status
        inc.status = to
        inc.resolved_at = datetime.now(UTC) if to == "resolved" else None
        payload = {"from": previous, "to": to, "note": note}
        await self.events.add(
            IncidentEvent(
                incident_id=inc.id,
                type="status_changed",
                actor="user",
                actor_user_id=self.ctx.user_id,
                payload=payload,
            )
        )
        self.audit.record(
            "incident.status_changed",
            meta,
            org_id=self.ctx.org_id,
            actor_user_id=self.ctx.user_id,
            target_type="incident",
            target_id=inc.id,
            metadata={"from": previous, "to": to, "number": inc.number},
        )
        await self.session.commit()
        await self.session.refresh(inc)
        return inc

    async def events_after(
        self, incident_id: uuid.UUID, *, after_id: int | None, limit: int
    ) -> tuple[list[IncidentEvent], str | None]:
        inc = await self.get(incident_id)
        rows = await self.events.after(inc.id, after_id, limit + 1)
        next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
        return rows[:limit], next_cursor
