"""Read-only aggregate queries behind the Overview page and a service's ingest usage.

Every query is tenant-scoped (TenantRepository) and bounded: by a service list (registry-sized,
capped by the caller), a time window, and a row limit. Aggregation happens in SQL; nothing
unbounded is loaded into memory.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select

from app.models.detection import Detection, ServiceWindowStats
from app.models.incident import Incident
from app.models.telemetry import IngestUsage
from app.repositories.base import TenantRepository


@dataclass(frozen=True, slots=True)
class UsageCell:
    minute: datetime
    signal: str
    outcome: str
    count: int


@dataclass(frozen=True, slots=True)
class MinuteStats:
    service_id: uuid.UUID
    minute: datetime
    requests: int
    errors: int


class IngestUsageQueries(TenantRepository[IngestUsage]):
    model = IngestUsage

    async def minutes(
        self,
        service_id: uuid.UUID,
        start: datetime,
        end: datetime,
        *,
        before: datetime | None,
        limit: int,
    ) -> list[datetime]:
        """Distinct minutes with usage in [start, end) (and < `before`), newest first.
        Uses `ix_ingest_usage_service_minute`."""
        stmt = (
            select(IngestUsage.minute)
            .where(
                self._org_col() == self.ctx.org_id,
                IngestUsage.service_id == service_id,
                IngestUsage.minute >= start,
                IngestUsage.minute < end,
            )
            .group_by(IngestUsage.minute)
            .order_by(IngestUsage.minute.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(IngestUsage.minute < before)
        return list((await self.session.scalars(stmt)).all())

    async def cells(self, service_id: uuid.UUID, minutes: Sequence[datetime]) -> list[UsageCell]:
        """Counts summed over the service's keys, per (minute, signal, outcome)."""
        if not minutes:
            return []
        stmt = (
            select(
                IngestUsage.minute,
                IngestUsage.signal,
                IngestUsage.outcome,
                func.sum(IngestUsage.count),
            )
            .where(
                self._org_col() == self.ctx.org_id,
                IngestUsage.service_id == service_id,
                IngestUsage.minute.in_(list(minutes)),
            )
            .group_by(IngestUsage.minute, IngestUsage.signal, IngestUsage.outcome)
        )
        return [UsageCell(m, s, o, int(c)) for m, s, o, c in (await self.session.execute(stmt))]

    async def totals_since(
        self, service_ids: Sequence[uuid.UUID], since: datetime
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """(accepted, rejected) records per service since `since`."""
        if not service_ids:
            return {}
        accepted = func.sum(case((IngestUsage.outcome == "accepted", IngestUsage.count), else_=0))
        rejected = func.sum(case((IngestUsage.outcome != "accepted", IngestUsage.count), else_=0))
        stmt = (
            select(IngestUsage.service_id, accepted, rejected)
            .where(
                self._org_col() == self.ctx.org_id,
                IngestUsage.service_id.in_(list(service_ids)),
                IngestUsage.minute >= since,
            )
            .group_by(IngestUsage.service_id)
        )
        return {sid: (int(a), int(r)) for sid, a, r in await self.session.execute(stmt)}


class WindowSeriesQueries(TenantRepository[ServiceWindowStats]):
    model = ServiceWindowStats

    async def per_minute(
        self, service_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> list[MinuteStats]:
        """Requests and errors per service and minute from the 15-s rollups in [start, end)."""
        if not service_ids:
            return []
        minute = func.date_trunc("minute", ServiceWindowStats.bucket_start).label("minute")
        stmt = (
            select(
                ServiceWindowStats.service_id,
                minute,
                func.sum(ServiceWindowStats.requests),
                func.sum(ServiceWindowStats.errors),
            )
            .where(
                self._org_col() == self.ctx.org_id,
                ServiceWindowStats.service_id.in_(list(service_ids)),
                ServiceWindowStats.bucket_start >= start,
                ServiceWindowStats.bucket_start < end,
            )
            .group_by(ServiceWindowStats.service_id, minute)
            .order_by(ServiceWindowStats.service_id, minute)
        )
        return [
            MinuteStats(sid, m, int(r), int(e)) for sid, m, r, e in await self.session.execute(stmt)
        ]


class OverviewIncidentQueries(TenantRepository[Incident]):
    model = Incident

    async def open_by_severity(self, project_id: uuid.UUID) -> dict[str, int]:
        """Unresolved incidents of the project by severity (partial index `ix_incidents_active`)."""
        stmt = (
            select(Incident.severity, func.count())
            .where(
                self._org_col() == self.ctx.org_id,
                Incident.project_id == project_id,
                Incident.status != "resolved",
            )
            .group_by(Incident.severity)
        )
        return {sev: int(n) for sev, n in await self.session.execute(stmt)}

    async def latest(self, project_id: uuid.UUID, limit: int) -> list[Incident]:
        stmt = (
            self.scoped()
            .where(Incident.project_id == project_id)
            .order_by(Incident.created_at.desc(), Incident.id.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())


class DetectionCountQueries(TenantRepository[Detection]):
    model = Detection

    async def by_rule_since(
        self, service_ids: Sequence[uuid.UUID], since: datetime
    ) -> dict[str, int]:
        """Detections that started since `since` on these services, per rule."""
        if not service_ids:
            return {}
        stmt = (
            select(Detection.rule, func.count())
            .where(
                self._org_col() == self.ctx.org_id,
                Detection.service_id.in_(list(service_ids)),
                Detection.started_at >= since,
            )
            .group_by(Detection.rule)
        )
        return {rule: int(n) for rule, n in await self.session.execute(stmt)}
