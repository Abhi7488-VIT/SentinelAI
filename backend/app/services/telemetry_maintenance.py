"""Telemetry partition maintenance: create daily partitions ahead, drop them after retention.

Runs from the `migrate` one-shot container (so partitions exist before anything ingests), every
10 minutes from Celery beat, and at ingest start-up. Safe to run concurrently: a transaction-scoped
advisory lock makes overlapping runs skip.

Window: partitions exist from (today - 1) to (today + days_ahead), UTC. Ingest accepts records up
to 2 h old and 5 min in the future (Phase 3 plan §3.2), so every accepted record has a partition.
A partition is dropped once its whole day is older than the table's retention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.telemetry import PARTITIONED_TABLES
from app.repositories import telemetry_partitions as parts

logger = get_logger("app.telemetry.maintenance")
BATCH_ID_RETENTION = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    ran: bool
    created: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    usage_rows_deleted: int = 0
    batch_ids_deleted: int = 0


def retention_days(settings: Settings) -> dict[str, int]:
    days = {
        "spans": settings.telemetry_retention_spans_days,
        "log_records": settings.telemetry_retention_logs_days,
        "metric_points": settings.telemetry_retention_metrics_days,
    }
    assert set(days) == set(PARTITIONED_TABLES)  # noqa: S101  (programming invariant)
    return days


async def run_maintenance(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> MaintenanceResult:
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    if not await parts.try_lock(session):
        await session.rollback()
        logger.info("telemetry_maintenance_skipped", reason="locked")
        return MaintenanceResult(ran=False)

    created: list[str] = []
    dropped: list[str] = []
    for parent, keep_days in retention_days(settings).items():
        existing = {p.day for p in await parts.list_partitions(session, parent)}
        for offset in range(-1, settings.telemetry_partition_days_ahead + 1):
            day = today + timedelta(days=offset)
            if day not in existing:
                await parts.create_partition(session, parent, day)
                created.append(parts.partition_name(parent, day))
        # A partition covers [day, day + 1); drop it when its end is older than the retention.
        oldest_kept = today - timedelta(days=keep_days)
        for partition in await parts.list_partitions(session, parent):
            if partition.day < oldest_kept:
                await parts.drop_partition(session, partition)
                dropped.append(partition.name)

    usage_deleted = await parts.delete_usage_before(
        session, now - timedelta(days=settings.ingest_usage_retention_days)
    )
    # Processed-batch ids only need to outlive any queue redelivery (hours); keep two days.
    batches_deleted = await parts.delete_batches_before(session, now - BATCH_ID_RETENTION)
    await session.commit()
    logger.info(
        "telemetry_maintenance_done",
        created=created,
        dropped=dropped,
        usage_rows_deleted=usage_deleted,
        batch_ids_deleted=batches_deleted,
    )
    return MaintenanceResult(
        ran=True,
        created=created,
        dropped=dropped,
        usage_rows_deleted=usage_deleted,
        batch_ids_deleted=batches_deleted,
    )
