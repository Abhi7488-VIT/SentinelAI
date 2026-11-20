"""Tenant-scoped repositories for normalized telemetry.

Write paths (Phase 3 T4) are idempotent bulk inserts (`ON CONFLICT DO NOTHING` on the dedup
primary keys), chunked to stay under Postgres' 32,767 bind-parameter limit. `org_id` always comes
from the repository's scope; service ids reach their tenant's service through composite FKs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.ingest.batch import DeploymentRow, LogRow, MetricRow, SpanRow
from app.models.service import Service, ServiceApiKey
from app.models.telemetry import (
    Deployment,
    IngestBatch,
    IngestUsage,
    LogRecord,
    MetricPoint,
    MetricSeries,
    Span,
)
from app.repositories.base import TenantRepository

_MAX_PARAMS = 32_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Throttle "last seen/used" bookkeeping so every batch does not rewrite the same row.
_TOUCH_GRANULARITY = timedelta(seconds=5)


def from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterable[Sequence[dict[str, Any]]]:
    if not rows:
        return
    size = max(1, _MAX_PARAMS // len(rows[0]))
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class _BulkInsert:
    """Mixin: `_insert_ignore(model, rows)` returns how many rows were actually inserted."""

    session: Any

    async def _insert_ignore(self, model: type[Any], rows: Sequence[dict[str, Any]]) -> int:
        inserted = 0
        for chunk in _chunks(rows):
            result = await self.session.execute(
                insert(model).values(list(chunk)).on_conflict_do_nothing()
            )
            inserted += int(getattr(result, "rowcount", 0) or 0)
        return inserted


class SpanRepository(_BulkInsert, TenantRepository[Span]):
    model = Span

    async def insert_many(self, service_id: uuid.UUID, rows: Sequence[SpanRow]) -> int:
        org = self.ctx.org_id
        return await self._insert_ignore(
            Span,
            [
                {
                    "org_id": org,
                    "service_id": service_id,
                    "trace_id": r.trace_id,
                    "span_id": r.span_id,
                    "parent_span_id": r.parent_span_id,
                    "start_time": from_us(r.start_us),
                    "end_time": from_us(r.end_us),
                    "duration_ms": r.duration_ms,
                    "name": r.name,
                    "kind": r.kind,
                    "status_code": r.status_code,
                    "status_message": r.status_message,
                    "http_method": r.http_method,
                    "http_route": r.http_route,
                    "http_status_code": r.http_status_code,
                    "db_system": r.db_system,
                    "server_address": r.server_address,
                    "peer_service": r.peer_service,
                    "exception_type": r.exception_type,
                    "exception_message": r.exception_message,
                    "service_instance_id": r.service_instance_id,
                    "service_version": r.service_version,
                    "attributes": r.attributes,
                }
                for r in rows
            ],
        )


class LogRecordRepository(_BulkInsert, TenantRepository[LogRecord]):
    model = LogRecord

    async def insert_many(self, service_id: uuid.UUID, rows: Sequence[LogRow]) -> int:
        org = self.ctx.org_id
        return await self._insert_ignore(
            LogRecord,
            [
                {
                    "org_id": org,
                    "service_id": service_id,
                    "record_hash": r.record_hash,
                    "ts": from_us(r.ts_us),
                    "observed_ts": from_us(r.observed_us) if r.observed_us else None,
                    "severity_number": r.severity_number,
                    "severity_text": r.severity_text,
                    "body": r.body,
                    "trace_id": r.trace_id,
                    "span_id": r.span_id,
                    "event_name": r.event_name,
                    "exception_type": r.exception_type,
                    "exception_message": r.exception_message,
                    "exception_stacktrace": r.exception_stacktrace,
                    "service_instance_id": r.service_instance_id,
                    "service_version": r.service_version,
                    "attributes": r.attributes,
                }
                for r in rows
            ],
        )


class MetricSeriesRepository(_BulkInsert, TenantRepository[MetricSeries]):
    model = MetricSeries

    async def ids_for(self, hashes: Sequence[bytes]) -> dict[bytes, int]:
        if not hashes:
            return {}
        stmt = select(MetricSeries.series_hash, MetricSeries.id).where(
            MetricSeries.org_id == self.ctx.org_id, MetricSeries.series_hash.in_(hashes)
        )
        return {bytes(h): int(i) for h, i in (await self.session.execute(stmt)).all()}

    async def count_for_service(self, service_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            MetricSeries.org_id == self.ctx.org_id, MetricSeries.service_id == service_id
        )
        return int(await self.session.scalar(stmt) or 0)

    async def insert_new(self, service_id: uuid.UUID, rows: Sequence[MetricRow]) -> None:
        await self._insert_ignore(
            MetricSeries,
            [
                {
                    "org_id": self.ctx.org_id,
                    "service_id": service_id,
                    "series_hash": r.series_hash,
                    "name": r.name,
                    "unit": r.unit,
                    "kind": r.kind,
                    "is_monotonic": r.is_monotonic,
                    "temporality": r.temporality,
                    "attributes": r.series_attributes,
                }
                for r in rows
            ],
        )

    async def touch(self, ids: Sequence[int], now: datetime) -> None:
        if not ids:
            return
        await self.session.execute(
            update(MetricSeries)
            .where(
                MetricSeries.org_id == self.ctx.org_id,
                MetricSeries.id.in_(ids),
                MetricSeries.last_seen_at < now - _TOUCH_GRANULARITY,
            )
            .values(last_seen_at=now)
        )


class MetricPointRepository(_BulkInsert, TenantRepository[MetricPoint]):
    model = MetricPoint

    async def insert_many(self, points: Sequence[tuple[int, MetricRow]]) -> int:
        org = self.ctx.org_id
        return await self._insert_ignore(
            MetricPoint,
            [
                {
                    "org_id": org,
                    "series_id": series_id,
                    "ts": from_us(r.ts_us),
                    "start_ts": from_us(r.start_us) if r.start_us else None,
                    "value": r.value,
                    "count": r.count,
                    "sum": r.sum,
                    "bucket_counts": r.bucket_counts,
                    "explicit_bounds": r.explicit_bounds,
                }
                for series_id, r in points
            ],
        )


class DeploymentRepository(_BulkInsert, TenantRepository[Deployment]):
    model = Deployment

    async def insert_many(self, service_id: uuid.UUID, rows: Sequence[DeploymentRow]) -> int:
        return await self._insert_ignore(
            Deployment,
            [
                {
                    "org_id": self.ctx.org_id,
                    "service_id": service_id,
                    "deployment_id": r.deployment_id,
                    "status": r.status,
                    "version": r.version,
                    "previous_version": r.previous_version,
                    "ts": from_us(r.ts_us),
                }
                for r in rows
            ],
        )


class IngestUsageRepository(TenantRepository[IngestUsage]):
    model = IngestUsage

    async def add_counts(
        self,
        *,
        key_id: uuid.UUID,
        service_id: uuid.UUID,
        signal: str,
        minute: datetime,
        counts: dict[str, int],
    ) -> None:
        rows = [
            {
                "org_id": self.ctx.org_id,
                "key_id": key_id,
                "service_id": service_id,
                "signal": signal,
                "minute": minute,
                "outcome": outcome,
                "count": n,
            }
            for outcome, n in counts.items()
            if n > 0
        ]
        if not rows:
            return
        stmt = insert(IngestUsage).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="pk_ingest_usage",
            set_={"count": IngestUsage.count + stmt.excluded.count},
        )
        await self.session.execute(stmt)


class IngestBatchRepository(TenantRepository[IngestBatch]):
    model = IngestBatch

    async def claim(self, batch_id: uuid.UUID) -> bool:
        """Record the batch as processed; False if it already was (a redelivered message)."""
        stmt = (
            insert(IngestBatch)
            .values(batch_id=batch_id, org_id=self.ctx.org_id)
            .on_conflict_do_nothing()
            .returning(IngestBatch.batch_id)
        )
        return (await self.session.execute(stmt)).first() is not None


async def touch_service_and_key(
    session: Any,
    *,
    org_id: uuid.UUID,
    service_id: uuid.UUID,
    key_id: uuid.UUID,
    seen_at: datetime,
    used_at: datetime,
) -> None:
    """Throttled bookkeeping. Explicit `updated_at` keeps user-edit timestamps untouched."""
    await session.execute(
        update(Service)
        .where(
            Service.org_id == org_id,
            Service.id == service_id,
            (Service.last_seen_at.is_(None))
            | (Service.last_seen_at < seen_at - _TOUCH_GRANULARITY),
        )
        .values(last_seen_at=seen_at, updated_at=Service.updated_at)
    )
    await session.execute(
        update(ServiceApiKey)
        .where(
            ServiceApiKey.org_id == org_id,
            ServiceApiKey.id == key_id,
            (ServiceApiKey.last_used_at.is_(None))
            | (ServiceApiKey.last_used_at < used_at - _TOUCH_GRANULARITY),
        )
        .values(last_used_at=used_at)
    )
