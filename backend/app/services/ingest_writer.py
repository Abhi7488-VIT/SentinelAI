"""Write one normalized ingest batch to Postgres — idempotently, in one transaction.

1. Claim `batch_id` in `ingest_batches`. If it was already claimed, a previous delivery of this
   message committed: skip everything (exactly-once effects, including usage counts).
2. Insert spans / logs / deployments / metric points with `ON CONFLICT DO NOTHING`, so telemetry a
   sender re-sends in a new batch is not duplicated either (invariant 6).
3. Metric cardinality guard: new series beyond `ingest_max_series_per_service` are not created;
   their points are dropped and counted as `series_limit`.
4. Upsert per-minute usage: `accepted` and `rejected:<reason>`.
5. Throttled `services.last_seen_at` / `service_api_keys.last_used_at`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.metrics import INGEST_ROWS_WRITTEN
from app.core.tenancy import SystemScope
from app.ingest.batch import MetricRow, NormalizedBatch
from app.repositories.telemetry import (
    DeploymentRepository,
    IngestBatchRepository,
    IngestUsageRepository,
    LogRecordRepository,
    MetricPointRepository,
    MetricSeriesRepository,
    SpanRepository,
    from_us,
    touch_service_and_key,
)


@dataclass(frozen=True, slots=True)
class WriteResult:
    skipped_duplicate_batch: bool
    inserted: dict[str, int]
    rejected: dict[str, int]


async def write_batch(
    session: AsyncSession, batch: NormalizedBatch, settings: Settings
) -> WriteResult:
    scope = SystemScope(org_id=batch.org_id, actor="ingest-writer")
    if not await IngestBatchRepository(session, scope).claim(batch.batch_id):
        await session.rollback()
        return WriteResult(skipped_duplicate_batch=True, inserted={}, rejected={})

    rejected = dict(batch.rejected)
    inserted = {
        "spans": await SpanRepository(session, scope).insert_many(batch.service_id, batch.spans),
        "log_records": await LogRecordRepository(session, scope).insert_many(
            batch.service_id, batch.logs
        ),
        "deployments": await DeploymentRepository(session, scope).insert_many(
            batch.service_id, batch.deployments
        ),
        "metric_points": 0,
    }
    dropped = 0
    if batch.metrics:
        inserted["metric_points"], dropped = await _write_metrics(session, scope, batch, settings)
        if dropped:
            rejected["series_limit"] = rejected.get("series_limit", 0) + dropped
    accepted = batch.accepted - dropped

    received = from_us(batch.received_us)
    minute = received.replace(second=0, microsecond=0)
    counts = {"accepted": accepted, **{f"rejected:{k}": v for k, v in rejected.items()}}
    await IngestUsageRepository(session, scope).add_counts(
        key_id=batch.key_id,
        service_id=batch.service_id,
        signal=batch.signal,
        minute=minute,
        counts=counts,
    )
    latest = _latest_record_time(batch)
    if latest is not None:
        await touch_service_and_key(
            session,
            org_id=batch.org_id,
            service_id=batch.service_id,
            key_id=batch.key_id,
            seen_at=latest,
            used_at=received,
        )
    await session.commit()
    for table, n in inserted.items():
        if n:
            INGEST_ROWS_WRITTEN.labels(table).inc(n)
    return WriteResult(skipped_duplicate_batch=False, inserted=inserted, rejected=rejected)


async def _write_metrics(
    session: AsyncSession, scope: SystemScope, batch: NormalizedBatch, settings: Settings
) -> tuple[int, int]:
    series_repo = MetricSeriesRepository(session, scope)
    by_hash: dict[bytes, list[MetricRow]] = defaultdict(list)
    for row in batch.metrics:
        by_hash[row.series_hash].append(row)

    known = await series_repo.ids_for(list(by_hash))
    new_hashes = [h for h in by_hash if h not in known]
    dropped = 0
    if new_hashes:
        room = settings.ingest_max_series_per_service - await series_repo.count_for_service(
            batch.service_id
        )
        allowed = new_hashes[: max(0, room)]
        for h in new_hashes[max(0, room) :]:
            dropped += len(by_hash.pop(h))
        await series_repo.insert_new(batch.service_id, [by_hash[h][0] for h in allowed])
        known.update(await series_repo.ids_for(allowed))

    points = [(known[h], row) for h, rows in by_hash.items() for row in rows]
    written = await MetricPointRepository(session, scope).insert_many(points)
    await series_repo.touch([known[h] for h in by_hash], datetime.now(UTC))
    return written, dropped


def _latest_record_time(batch: NormalizedBatch) -> datetime | None:
    times = [
        *(s.start_us for s in batch.spans),
        *(r.ts_us for r in batch.logs),
        *(m.ts_us for m in batch.metrics),
    ]
    return from_us(max(times)) if times else None
