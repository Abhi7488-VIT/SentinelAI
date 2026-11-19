"""Incident repositories (Phase 3 T8). Tenant-scoped; the correlator uses them via SystemScope."""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.models.detection import Detection
from app.models.graph import ServiceEdge
from app.models.incident import Incident, IncidentDetection, IncidentEvent, IncidentService
from app.models.telemetry import Span
from app.repositories.base import TenantRepository

_NUMBER_QUERY = re.compile(r"^#?(\d{1,9})$")


class IncidentRepository(TenantRepository[Incident]):
    model = Incident

    async def next_number(self) -> int:
        """Per-org sequence. Callers hold the detection tick's lock (single writer); the
        (org_id, number) unique constraint backs it up."""
        current = await self.session.scalar(
            select(func.max(Incident.number)).where(Incident.org_id == self.ctx.org_id)
        )
        return int(current or 0) + 1

    async def active_in_project(self, project_id: uuid.UUID, since: datetime) -> list[Incident]:
        """Unresolved incidents of the project active since `since`; oldest first."""
        stmt = (
            self.scoped()
            .where(
                Incident.project_id == project_id,
                Incident.status != "resolved",
                Incident.last_activity_at >= since,
            )
            .order_by(Incident.created_at, Incident.number)
        )
        return list((await self.session.scalars(stmt)).all())

    async def unresolved(self) -> list[Incident]:
        stmt = self.scoped().where(Incident.status != "resolved").order_by(Incident.number)
        return list((await self.session.scalars(stmt)).all())

    async def get_for_update(self, incident_id: uuid.UUID) -> Incident | None:
        stmt = self.scoped().where(Incident.id == incident_id).with_for_update()
        return (await self.session.scalars(stmt)).first()

    async def list_filtered(
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
    ) -> tuple[list[Incident], str | None]:
        where: list[ColumnElement[bool]] = []
        if project_id is not None:
            where.append(Incident.project_id == project_id)
        if status is not None:
            where.append(Incident.status == status)
        if severity is not None:
            where.append(Incident.severity == severity)
        if start is not None:
            where.append(Incident.started_at >= start)
        if end is not None:
            where.append(Incident.started_at < end)
        if service_id is not None:
            where.append(
                exists().where(
                    IncidentService.org_id == Incident.org_id,
                    IncidentService.incident_id == Incident.id,
                    IncidentService.service_id == service_id,
                )
            )
        if q:
            number = _NUMBER_QUERY.match(q.strip())
            # ILIKE with a literal pattern (wildcards in user input escaped) → trigram index.
            pattern = "%" + re.sub(r"([%_\\])", r"\\\1", q.strip()) + "%"
            title_match = Incident.title.ilike(pattern, escape="\\")
            where.append(
                or_(title_match, Incident.number == int(number.group(1))) if number else title_match
            )
        return await self.list_page(limit=limit, cursor=cursor, where=where)


class IncidentDetectionRepository(TenantRepository[IncidentDetection]):
    model = IncidentDetection
    id_column = "detection_id"

    async def link(
        self,
        incident_id: uuid.UUID,
        detection_id: uuid.UUID,
        reasons: list[dict[str, object]],
        score: int,
    ) -> bool:
        """Attach a detection (once: its id is the key). False if it was already linked."""
        stmt = (
            insert(IncidentDetection)
            .values(
                org_id=self.ctx.org_id,
                incident_id=incident_id,
                detection_id=detection_id,
                reasons=reasons,
                score=score,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "detection_id"])
            .returning(IncidentDetection.detection_id)
        )
        return await self.session.scalar(stmt) is not None

    async def unlinked_detections(self, since: datetime) -> list[Detection]:
        """Detections fired since `since` that are in no incident yet (oldest first)."""
        stmt = (
            select(Detection)
            .where(
                Detection.org_id == self.ctx.org_id,
                Detection.fired_at >= since,
                ~exists().where(
                    IncidentDetection.org_id == Detection.org_id,
                    IncidentDetection.detection_id == Detection.id,
                ),
            )
            # Stable order within a tick (not the random detection id).
            .order_by(
                Detection.fired_at, Detection.started_at, Detection.service_id, Detection.rule
            )
        )
        return list((await self.session.scalars(stmt)).all())

    async def members(
        self, incident_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[Detection, IncidentDetection]]]:
        out: dict[uuid.UUID, list[tuple[Detection, IncidentDetection]]] = defaultdict(list)
        if not incident_ids:
            return out
        stmt = (
            select(Detection, IncidentDetection)
            .join(
                IncidentDetection,
                and_(
                    IncidentDetection.org_id == Detection.org_id,
                    IncidentDetection.detection_id == Detection.id,
                ),
            )
            .where(
                IncidentDetection.org_id == self.ctx.org_id,
                IncidentDetection.incident_id.in_(incident_ids),
            )
            .order_by(Detection.started_at, Detection.id)
        )
        for det, link in (await self.session.execute(stmt)).all():
            out[link.incident_id].append((det, link))
        return out


class IncidentServiceRepository(TenantRepository[IncidentService]):
    model = IncidentService
    id_column = "service_id"

    async def add_service(self, incident_id: uuid.UUID, service_id: uuid.UUID) -> None:
        await self.session.execute(
            insert(IncidentService)
            .values(org_id=self.ctx.org_id, incident_id=incident_id, service_id=service_id)
            .on_conflict_do_nothing()
        )

    async def of(self, incident_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
        out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        if not incident_ids:
            return out
        stmt = (
            select(IncidentService.incident_id, IncidentService.service_id)
            .where(
                IncidentService.org_id == self.ctx.org_id,
                IncidentService.incident_id.in_(incident_ids),
            )
            .order_by(IncidentService.created_at, IncidentService.service_id)
        )
        for inc, svc in (await self.session.execute(stmt)).all():
            out[inc].append(svc)
        return out


class IncidentEventRepository(TenantRepository[IncidentEvent]):
    model = IncidentEvent

    async def after(
        self, incident_id: uuid.UUID, after_id: int | None, limit: int
    ) -> list[IncidentEvent]:
        stmt = self.scoped().where(IncidentEvent.incident_id == incident_id)
        if after_id is not None:
            stmt = stmt.where(IncidentEvent.id > after_id)
        stmt = stmt.order_by(IncidentEvent.id).limit(limit)
        return list((await self.session.scalars(stmt)).all())


class CorrelationQueries(TenantRepository[Span]):
    """Bounded span/edge lookups for correlation reasons."""

    model = Span

    async def traces_with_errors(
        self,
        trace_ids: Sequence[bytes],
        service_ids: Sequence[uuid.UUID],
        since: datetime,
        limit: int = 5,
    ) -> list[bytes]:
        """Which of `trace_ids` (≤ 200) contain an ERROR span of one of `service_ids`."""
        if not trace_ids or not service_ids:
            return []
        rows = await self.session.execute(
            text(
                "SELECT DISTINCT trace_id FROM spans"
                " WHERE org_id = :org AND trace_id = ANY(:traces) AND service_id = ANY(:svcs)"
                "   AND status_code = 2 AND start_time >= :since"
                " LIMIT :limit"
            ),
            {
                "org": self.ctx.org_id,
                "traces": list(trace_ids)[:200],
                "svcs": list(service_ids),
                "since": since,
                "limit": limit,
            },
        )
        return sorted(bytes(r[0]) for r in rows)

    async def edges_touching(
        self, service_id: uuid.UUID, others: Sequence[uuid.UUID]
    ) -> list[ServiceEdge]:
        """Direct edges (either direction, any source) between `service_id` and `others`."""
        if not others:
            return []
        stmt = (
            select(ServiceEdge)
            .where(
                ServiceEdge.org_id == self.ctx.org_id,
                or_(
                    and_(
                        ServiceEdge.source_service_id == service_id,
                        ServiceEdge.target_service_id.in_(others),
                    ),
                    and_(
                        ServiceEdge.target_service_id == service_id,
                        ServiceEdge.source_service_id.in_(others),
                    ),
                ),
            )
            .order_by(
                ServiceEdge.source_service_id, ServiceEdge.target_service_id, ServiceEdge.source
            )
        )
        return list((await self.session.scalars(stmt)).all())
