"""Overview of a project and per-service ingest usage (read side for the UI).

Numbers come from stored data only (incidents, detections, 15-s rollups, ingest usage counters);
nothing is estimated. A service with no rollups simply has no series points.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidRequestError
from app.core.pagination import decode_cursor, encode_cursor
from app.core.tenancy import TenantContext
from app.models.incident import Incident
from app.models.project import Project
from app.models.service import Service
from app.repositories.overview import (
    DetectionCountQueries,
    IngestUsageQueries,
    MinuteStats,
    OverviewIncidentQueries,
    WindowSeriesQueries,
)
from app.repositories.services import ServiceRepository
from app.services.projects import ProjectService
from app.services.service_registry import ServiceRegistryService

SERIES_WINDOW = timedelta(minutes=60)
INGEST_WINDOW = timedelta(minutes=15)
DETECTIONS_WINDOW = timedelta(hours=24)
LATEST_INCIDENTS = 5
# Services shown on the Overview (registry-sized; beyond this the response says `truncated`).
MAX_OVERVIEW_SERVICES = 50

USAGE_DEFAULT_WINDOW = timedelta(hours=1)
USAGE_MAX_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ServiceSummary:
    service: Service
    accepted_15m: int
    rejected_15m: int
    series: list[MinuteStats]


@dataclass(frozen=True, slots=True)
class OverviewView:
    project: Project
    generated_at: datetime
    open_incidents: dict[str, int]
    latest_incidents: list[Incident]
    detections_24h: dict[str, int]
    services: list[ServiceSummary]
    services_truncated: bool


@dataclass(slots=True)
class UsageMinute:
    minute: datetime
    accepted: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    rejected_by_reason: dict[str, int] = field(default_factory=dict)


class OverviewService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.session = session
        self.ctx = ctx

    async def get(self, project_id: uuid.UUID, *, now: datetime | None = None) -> OverviewView:
        now = now or datetime.now(UTC)
        project = await ProjectService(self.session, self.ctx).get(project_id)  # 404 if foreign
        services = sorted(
            await ServiceRepository(self.session, self.ctx).active_in_project(project.id),
            key=lambda s: (s.name, s.environment, s.id),
        )
        truncated = len(services) > MAX_OVERVIEW_SERVICES
        services = services[:MAX_OVERVIEW_SERVICES]
        ids = [s.id for s in services]

        incidents = OverviewIncidentQueries(self.session, self.ctx)
        usage = await IngestUsageQueries(self.session, self.ctx).totals_since(
            ids, now - INGEST_WINDOW
        )
        series_rows = await WindowSeriesQueries(self.session, self.ctx).per_minute(
            ids, now - SERIES_WINDOW, now
        )
        series: dict[uuid.UUID, list[MinuteStats]] = defaultdict(list)
        for row in series_rows:
            series[row.service_id].append(row)
        return OverviewView(
            project=project,
            generated_at=now,
            open_incidents=await incidents.open_by_severity(project.id),
            latest_incidents=await incidents.latest(project.id, LATEST_INCIDENTS),
            detections_24h=await DetectionCountQueries(self.session, self.ctx).by_rule_since(
                ids, now - DETECTIONS_WINDOW
            ),
            services=[
                ServiceSummary(s, *usage.get(s.id, (0, 0)), series=series.get(s.id, []))
                for s in services
            ],
            services_truncated=truncated,
        )

    async def ingest_usage(
        self,
        service_id: uuid.UUID,
        *,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        cursor: str | None,
        now: datetime | None = None,
    ) -> tuple[list[UsageMinute], str | None]:
        """Per-minute usage of one service (summed over its keys), newest first, keyset-paged."""
        await ServiceRegistryService(self.session, self.ctx).get(service_id)  # 404 if foreign
        end = end or (now or datetime.now(UTC))
        start = start or end - USAGE_DEFAULT_WINDOW
        if start >= end:
            raise InvalidRequestError("`from` must be before `to`.", code="invalid_time_range")
        if end - start > USAGE_MAX_WINDOW:
            raise InvalidRequestError(
                "The time range may span at most 24 hours.", code="invalid_time_range"
            )
        before = decode_cursor(cursor).created_at if cursor else None
        queries = IngestUsageQueries(self.session, self.ctx)
        minutes = await queries.minutes(service_id, start, end, before=before, limit=limit + 1)
        has_more = len(minutes) > limit
        minutes = minutes[:limit]
        by_minute = {m: UsageMinute(minute=m) for m in minutes}
        for cell in await queries.cells(service_id, minutes):
            item = by_minute[cell.minute]
            if cell.outcome == "accepted":
                item.accepted[cell.signal] = item.accepted.get(cell.signal, 0) + cell.count
            else:
                reason = cell.outcome.removeprefix("rejected:")
                item.rejected[cell.signal] = item.rejected.get(cell.signal, 0) + cell.count
                item.rejected_by_reason[reason] = (
                    item.rejected_by_reason.get(reason, 0) + cell.count
                )
        next_cursor = encode_cursor(minutes[-1], "minute") if has_more and minutes else None
        return [by_minute[m] for m in minutes], next_cursor
