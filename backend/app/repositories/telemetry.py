"""Tenant-scoped repositories for normalized telemetry.

Phase 3 T2 defines the storage; the write path (T4: batch inserts with ON CONFLICT DO NOTHING)
and the read paths used by rollups/detection (T7) are added to these classes in their tasks.
"""

from __future__ import annotations

from app.models.telemetry import (
    Deployment,
    IngestUsage,
    LogRecord,
    MetricPoint,
    MetricSeries,
    Span,
)
from app.repositories.base import TenantRepository


class SpanRepository(TenantRepository[Span]):
    model = Span


class LogRecordRepository(TenantRepository[LogRecord]):
    model = LogRecord


class MetricSeriesRepository(TenantRepository[MetricSeries]):
    model = MetricSeries


class MetricPointRepository(TenantRepository[MetricPoint]):
    model = MetricPoint


class DeploymentRepository(TenantRepository[Deployment]):
    model = Deployment


class IngestUsageRepository(TenantRepository[IngestUsage]):
    model = IngestUsage
