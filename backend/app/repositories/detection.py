"""Detection repositories (Phase 3 T7). All tenant-scoped; the two cross-tenant helpers at the
bottom return org ids or run retention for the background job only.

Every telemetry read is anchored on (org, service, time window) and uses the per-partition
indexes from the Phase 3 plan §3.2 (server-span window, error-span window, log window, metric
series by name + point primary key).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.detection import Detection, DetectorState, ServiceInstance, ServiceWindowStats
from app.models.telemetry import Deployment, MetricPoint, MetricSeries, Span
from app.repositories.base import TenantRepository

BUCKET = timedelta(seconds=15)
# Arbitrary constant: the detection tick's transaction-scoped advisory lock id.
TICK_LOCK_ID = 0x5E471_0003

_ROLLUP_SQL = text(
    """
    WITH b AS (
        SELECT generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                               interval '15 seconds') AS bucket_start
    )
    SELECT b.bucket_start,
        count(*) FILTER (WHERE s.start_time >= b.bucket_start) AS requests,
        count(*) FILTER (WHERE s.start_time >= b.bucket_start AND s.status_code = 2) AS errors,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY s.duration_ms)
            FILTER (WHERE s.start_time >= b.bucket_start) AS p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY s.duration_ms)
            FILTER (WHERE s.start_time >= b.bucket_start) AS p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY s.duration_ms)
            FILTER (WHERE s.start_time >= b.bucket_start) AS p99,
        count(*) AS requests_60s,
        count(*) FILTER (WHERE s.status_code = 2) AS errors_60s,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY s.duration_ms) AS p95_60s
    FROM b
    JOIN spans s
      ON s.org_id = :org AND s.service_id = :svc AND s.kind = 2
     AND s.start_time >= b.bucket_start - interval '45 seconds'
     AND s.start_time < b.bucket_start + interval '15 seconds'
     AND s.start_time >= CAST(:first AS timestamptz) - interval '45 seconds'
     AND s.start_time < CAST(:last AS timestamptz) + interval '15 seconds'
    GROUP BY b.bucket_start
    ORDER BY b.bucket_start
    """
)

_EXCEPTIONS_SQL = text(
    """
    SELECT date_bin('15 seconds', start_time, TIMESTAMPTZ '1970-01-01 00:00:00+00') AS bucket,
           exception_type, exception_message, count(*) AS n
    FROM spans
    WHERE org_id = :org AND service_id = :svc AND status_code = 2
      AND start_time >= :start AND start_time < :end
      AND (exception_type IS NOT NULL OR exception_message IS NOT NULL)
    GROUP BY 1, 2, 3
    ORDER BY n DESC
    LIMIT 500
    """
)

_INSTANCES_SQL = text(
    """
    INSERT INTO service_instances (org_id, service_id, instance_id, first_seen_at, last_seen_at)
    SELECT :org, :svc, iid, min(first_t), max(last_t) FROM (
        SELECT left(service_instance_id, 128) AS iid, start_time AS first_t, start_time AS last_t
        FROM spans
        WHERE org_id = :org AND service_id = :svc AND kind = 2
          AND start_time >= :start AND start_time < :end AND service_instance_id IS NOT NULL
        UNION ALL
        SELECT left(service_instance_id, 128), ts, ts
        FROM log_records
        WHERE org_id = :org AND service_id = :svc
          AND ts >= :start AND ts < :end AND service_instance_id IS NOT NULL
        UNION ALL
        SELECT left(attributes ->> 'service.instance.id', 128), created_at, last_seen_at
        FROM metric_series
        WHERE org_id = :org AND service_id = :svc AND last_seen_at >= :start
          AND attributes ? 'service.instance.id'
    ) x
    WHERE iid <> ''
    GROUP BY iid
    ORDER BY min(first_t) DESC
    LIMIT 1000
    ON CONFLICT (org_id, service_id, instance_id) DO UPDATE SET
        first_seen_at = least(service_instances.first_seen_at, excluded.first_seen_at),
        last_seen_at = greatest(service_instances.last_seen_at, excluded.last_seen_at)
    """
)


@dataclass(frozen=True, slots=True)
class BucketStats:
    bucket_start: datetime
    requests: int
    errors: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    requests_60s: int
    errors_60s: int
    p95_60s_ms: float | None


@dataclass(frozen=True, slots=True)
class ExceptionCount:
    bucket: datetime
    type: str | None
    message: str | None
    count: int


@dataclass(frozen=True, slots=True)
class MetricReading:
    name: str
    attributes: dict[str, Any]
    value: float


async def try_tick_lock(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": TICK_LOCK_ID})
    )


class WindowStatsRepository(TenantRepository[ServiceWindowStats]):
    model = ServiceWindowStats

    async def compute(
        self, service_id: uuid.UUID, first_bucket: datetime, last_bucket: datetime
    ) -> list[BucketStats]:
        """Stats for buckets [first_bucket, last_bucket] (inclusive starts), from spans."""
        rows = await self.session.execute(
            _ROLLUP_SQL,
            {"org": self.ctx.org_id, "svc": service_id, "first": first_bucket, "last": last_bucket},
        )
        return [BucketStats(*r) for r in rows]

    async def exception_counts(
        self, service_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[ExceptionCount]:
        rows = await self.session.execute(
            _EXCEPTIONS_SQL,
            {"org": self.ctx.org_id, "svc": service_id, "start": start, "end": end},
        )
        return [ExceptionCount(r[0], r[1], r[2], int(r[3])) for r in rows]

    async def upsert(
        self, service_id: uuid.UUID, stats: Sequence[BucketStats], exceptions: dict[datetime, Any]
    ) -> None:
        if not stats:
            return
        rows = [
            {
                "org_id": self.ctx.org_id,
                "service_id": service_id,
                "bucket_start": s.bucket_start,
                "requests": s.requests,
                "errors": s.errors,
                "p50_ms": s.p50_ms,
                "p95_ms": s.p95_ms,
                "p99_ms": s.p99_ms,
                "requests_60s": s.requests_60s,
                "errors_60s": s.errors_60s,
                "p95_60s_ms": s.p95_60s_ms,
                "exceptions": exceptions.get(s.bucket_start, []),
            }
            for s in stats
        ]
        stmt = insert(ServiceWindowStats).values(rows)
        cols = (
            "requests",
            "errors",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "requests_60s",
            "errors_60s",
            "p95_60s_ms",
            "exceptions",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["org_id", "service_id", "bucket_start"],
            set_={**{c: stmt.excluded[c] for c in cols}, "computed_at": func.now()},
        )
        await self.session.execute(stmt)

    async def latest_bucket(self, service_id: uuid.UUID) -> datetime | None:
        stmt = select(func.max(ServiceWindowStats.bucket_start)).where(
            ServiceWindowStats.org_id == self.ctx.org_id,
            ServiceWindowStats.service_id == service_id,
        )
        latest: datetime | None = await self.session.scalar(stmt)
        return latest

    async def at(self, service_id: uuid.UUID, bucket_start: datetime) -> ServiceWindowStats | None:
        stmt = self.scoped().where(
            ServiceWindowStats.service_id == service_id,
            ServiceWindowStats.bucket_start == bucket_start,
        )
        return (await self.session.scalars(stmt)).first()

    async def between(
        self, service_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[ServiceWindowStats]:
        """Buckets with bucket_start in [start, end), oldest first."""
        stmt = (
            self.scoped()
            .where(
                ServiceWindowStats.service_id == service_id,
                ServiceWindowStats.bucket_start >= start,
                ServiceWindowStats.bucket_start < end,
            )
            .order_by(ServiceWindowStats.bucket_start)
        )
        return list((await self.session.scalars(stmt)).all())

    async def totals_since(
        self, service_ids: Sequence[uuid.UUID], since: datetime
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """(requests, errors) per service from bucket-local counts since `since`."""
        if not service_ids:
            return {}
        stmt = (
            select(
                ServiceWindowStats.service_id,
                func.coalesce(func.sum(ServiceWindowStats.requests), 0),
                func.coalesce(func.sum(ServiceWindowStats.errors), 0),
            )
            .where(
                ServiceWindowStats.org_id == self.ctx.org_id,
                ServiceWindowStats.service_id.in_(service_ids),
                ServiceWindowStats.bucket_start >= since,
            )
            .group_by(ServiceWindowStats.service_id)
        )
        return {r[0]: (int(r[1]), int(r[2])) for r in (await self.session.execute(stmt)).all()}


class ServiceInstanceRepository(TenantRepository[ServiceInstance]):
    model = ServiceInstance

    async def observe(self, service_id: uuid.UUID, start: datetime, end: datetime) -> None:
        await self.session.execute(
            _INSTANCES_SQL,
            {"org": self.ctx.org_id, "svc": service_id, "start": start, "end": end},
        )

    async def first_seen_since(self, service_id: uuid.UUID, since: datetime) -> list[str]:
        stmt = (
            select(ServiceInstance.instance_id)
            .where(
                ServiceInstance.org_id == self.ctx.org_id,
                ServiceInstance.service_id == service_id,
                ServiceInstance.first_seen_at >= since,
            )
            .order_by(ServiceInstance.first_seen_at.desc())
            .limit(50)
        )
        return list((await self.session.scalars(stmt)).all())


class SpanEvidenceQueries(TenantRepository[Span]):
    """Sample trace ids for a detection's evidence (≤ 20)."""

    model = Span

    async def error_traces(
        self, service_id: uuid.UUID, start: datetime, end: datetime, limit: int = 20
    ) -> list[str]:
        rows = await self.session.execute(
            text(
                "SELECT DISTINCT ON (trace_id) trace_id, start_time FROM ("
                "  SELECT trace_id, start_time FROM spans"
                "  WHERE org_id = :org AND service_id = :svc AND status_code = 2"
                "    AND start_time >= :start AND start_time < :end"
                "  ORDER BY start_time DESC LIMIT 200) x "
                "ORDER BY trace_id, start_time DESC"
            ),
            {"org": self.ctx.org_id, "svc": service_id, "start": start, "end": end},
        )
        ordered = sorted(rows.all(), key=lambda r: r[1], reverse=True)
        return [bytes(r[0]).hex() for r in ordered[:limit]]

    async def slowest_traces(
        self, service_id: uuid.UUID, start: datetime, end: datetime, limit: int = 20
    ) -> list[str]:
        rows = await self.session.execute(
            text(
                "SELECT trace_id FROM spans"
                " WHERE org_id = :org AND service_id = :svc AND kind = 2"
                "   AND start_time >= :start AND start_time < :end"
                " ORDER BY duration_ms DESC LIMIT :limit"
            ),
            {"org": self.ctx.org_id, "svc": service_id, "start": start, "end": end, "limit": limit},
        )
        out: list[str] = []
        for (trace,) in rows:
            hexid = bytes(trace).hex()
            if hexid not in out:
                out.append(hexid)
        return out


class MetricReadingQueries(TenantRepository[MetricSeries]):
    model = MetricSeries

    async def latest(
        self, service_id: uuid.UUID, names: Sequence[str], start: datetime, end: datetime
    ) -> list[MetricReading]:
        """Latest gauge/sum value per series of these metric names within [start, end)."""
        stmt = (
            select(MetricSeries.name, MetricSeries.attributes, MetricPoint.value)
            .join(
                MetricPoint,
                and_(
                    MetricPoint.org_id == MetricSeries.org_id,
                    MetricPoint.series_id == MetricSeries.id,
                ),
            )
            .where(
                MetricSeries.org_id == self.ctx.org_id,
                MetricSeries.service_id == service_id,
                MetricSeries.name.in_(names),
                MetricPoint.org_id == self.ctx.org_id,
                MetricPoint.ts >= start,
                MetricPoint.ts < end,
                MetricPoint.value.is_not(None),
            )
            .distinct(MetricPoint.series_id)
            .order_by(MetricPoint.series_id, MetricPoint.ts.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [MetricReading(r[0], dict(r[1] or {}), float(r[2])) for r in rows]


class DeploymentQueries(TenantRepository[Deployment]):
    model = Deployment

    async def between(
        self, service_ids: Sequence[uuid.UUID], start: datetime, end: datetime
    ) -> list[Deployment]:
        """Earliest event per deployment id (its start) for these services in [start, end]."""
        if not service_ids:
            return []
        stmt = (
            self.scoped()
            .where(
                Deployment.service_id.in_(service_ids),
                Deployment.ts >= start,
                Deployment.ts <= end,
            )
            .order_by(Deployment.service_id, Deployment.deployment_id, Deployment.ts)
        )
        first: dict[tuple[uuid.UUID, str], Deployment] = {}
        for d in (await self.session.scalars(stmt)).all():
            first.setdefault((d.service_id, d.deployment_id), d)
        return sorted(first.values(), key=lambda d: (d.ts, d.deployment_id))


class DetectorStateRepository(TenantRepository[DetectorState]):
    model = DetectorState

    async def for_service(self, service_id: uuid.UUID) -> dict[str, DetectorState]:
        stmt = self.scoped().where(DetectorState.service_id == service_id)
        return {s.rule: s for s in (await self.session.scalars(stmt)).all()}

    async def list_filtered(
        self, *, limit: int, cursor: str | None, service_id: uuid.UUID | None
    ) -> tuple[list[DetectorState], str | None]:
        where: list[ColumnElement[bool]] = []
        if service_id is not None:
            where.append(DetectorState.service_id == service_id)
        return await self.list_page(limit=limit, cursor=cursor, where=where)


class DetectionRepository(TenantRepository[Detection]):
    model = Detection

    async def insert_once(self, values: dict[str, Any]) -> Detection:
        """Insert unless (service, rule, started_at) exists; return the stored row either way."""
        stmt = (
            insert(Detection)
            .values(org_id=self.ctx.org_id, **values)
            .on_conflict_do_nothing(index_elements=["org_id", "service_id", "rule", "started_at"])
            .returning(Detection.id)
        )
        new_id = await self.session.scalar(stmt)
        if new_id is None:
            existing = await self.session.scalar(
                self.scoped().where(
                    Detection.service_id == values["service_id"],
                    Detection.rule == values["rule"],
                    Detection.started_at == values["started_at"],
                )
            )
            assert existing is not None  # noqa: S101  (conflict implies the row exists)
            return existing
        row = await self.get(new_id)
        assert row is not None  # noqa: S101
        return row

    async def open_in_org(self) -> list[Detection]:
        stmt = self.scoped().where(Detection.resolved_at.is_(None)).order_by(Detection.fired_at)
        return list((await self.session.scalars(stmt)).all())

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Detection]:
        if not ids:
            return []
        return list((await self.session.scalars(self.scoped().where(Detection.id.in_(ids)))).all())

    async def intervals(
        self, service_id: uuid.UUID, rule: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime | None]]:
        """(started_at, resolved_at) of this rule's detections overlapping [start, end)."""
        stmt = select(Detection.started_at, Detection.resolved_at).where(
            Detection.org_id == self.ctx.org_id,
            Detection.service_id == service_id,
            Detection.rule == rule,
            Detection.started_at < end,
            (Detection.resolved_at.is_(None)) | (Detection.resolved_at >= start),
        )
        return [(r[0], r[1]) for r in (await self.session.execute(stmt)).all()]

    async def list_filtered(
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
        where: list[ColumnElement[bool]] = []
        if service_id is not None:
            where.append(Detection.service_id == service_id)
        if rule is not None:
            where.append(Detection.rule == rule)
        if status == "open":
            where.append(Detection.resolved_at.is_(None))
        elif status == "resolved":
            where.append(Detection.resolved_at.is_not(None))
        if start is not None:
            where.append(Detection.started_at >= start)
        if end is not None:
            where.append(Detection.started_at < end)
        return await self.list_page(limit=limit, cursor=cursor, where=where)


# ---- system (cross-tenant) helpers for the background job ----------------------------------


async def orgs_with_open_detections(session: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Detection.org_id).where(Detection.resolved_at.is_(None)).distinct()
    return list((await session.scalars(stmt)).all())


async def delete_rollups_before(session: AsyncSession, cutoff: datetime) -> tuple[int, int]:
    stats = await session.execute(
        delete(ServiceWindowStats).where(ServiceWindowStats.bucket_start < cutoff)
    )
    instances = await session.execute(
        delete(ServiceInstance).where(ServiceInstance.last_seen_at < cutoff)
    )
    return (
        int(getattr(stats, "rowcount", 0) or 0),
        int(getattr(instances, "rowcount", 0) or 0),
    )
