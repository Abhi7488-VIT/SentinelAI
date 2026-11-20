"""DDL and housekeeping queries for day-partitioned telemetry tables (ADR 0005).

Not tenant-scoped: partitions are physical storage shared by all tenants. Table names come only
from the fixed `PARTITIONED_TABLES` allow-list and dates are formatted from `date` objects, so the
DDL strings built here never contain caller-controlled text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telemetry import PARTITIONED_TABLES, IngestBatch, IngestUsage

# Arbitrary constant: the maintenance job's transaction-scoped advisory lock id.
MAINTENANCE_LOCK_ID = 0x5E471_0001
_PARTITION_RE = re.compile(r"^(?P<parent>[a-z_]+)_p(?P<day>\d{8})$")


@dataclass(frozen=True, slots=True)
class Partition:
    parent: str
    name: str
    day: date


def partition_name(parent: str, day: date) -> str:
    _check_parent(parent)
    return f"{parent}_p{day:%Y%m%d}"


def _check_parent(parent: str) -> None:
    if parent not in PARTITIONED_TABLES:
        raise ValueError(f"not a partitioned telemetry table: {parent!r}")


def _day_start(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=UTC).isoformat()


async def try_lock(session: AsyncSession) -> bool:
    """Transaction-scoped advisory lock; released at commit/rollback."""
    return bool(
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": MAINTENANCE_LOCK_ID}
        )
    )


async def list_partitions(session: AsyncSession, parent: str) -> list[Partition]:
    """Partitions of `parent` that follow our naming scheme (others are never touched)."""
    _check_parent(parent)
    rows = await session.execute(
        text(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = :parent"
        ),
        {"parent": parent},
    )
    out: list[Partition] = []
    for (name,) in rows:
        match = _PARTITION_RE.match(name)
        if match and match.group("parent") == parent:
            day = datetime.strptime(match.group("day"), "%Y%m%d").replace(tzinfo=UTC).date()
            out.append(Partition(parent=parent, name=name, day=day))
    return sorted(out, key=lambda p: p.day)


async def create_partition(session: AsyncSession, parent: str, day: date) -> None:
    name = partition_name(parent, day)
    # Indexes and constraints defined on the parent are created on the partition automatically.
    await session.execute(
        text(
            f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{parent}" '
            f"FOR VALUES FROM ('{_day_start(day)}') TO ('{_day_start(day + timedelta(days=1))}')"
        )
    )


async def drop_partition(session: AsyncSession, partition: Partition) -> None:
    if partition_name(partition.parent, partition.day) != partition.name:
        raise ValueError(f"unexpected partition name: {partition.name!r}")
    await session.execute(text(f'DROP TABLE IF EXISTS "{partition.name}"'))


async def delete_usage_before(session: AsyncSession, cutoff: datetime) -> int:
    result = await session.execute(delete(IngestUsage).where(IngestUsage.minute < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_batches_before(session: AsyncSession, cutoff: datetime) -> int:
    result = await session.execute(delete(IngestBatch).where(IngestBatch.processed_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)
