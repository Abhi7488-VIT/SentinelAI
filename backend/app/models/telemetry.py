"""Normalized telemetry storage (ADR 0005).

`spans`, `log_records` and `metric_points` are RANGE-partitioned by day on their timestamp. The
parents are created by migrations; daily partitions are created ahead of time and dropped after
their retention by `app.services.telemetry_maintenance` (no DEFAULT partition, so an insert outside
the prepared window fails loudly instead of landing somewhere unbounded).

Dedup keys (the primary keys) make every insert idempotent (`ON CONFLICT DO NOTHING`), so a
retried OTLP batch or a redelivered queue message never duplicates rows (invariant 6). Postgres
requires the partition column in every unique key; it is part of each record's identity anyway.

Every row reaches its service through a composite FK `(org_id, service_id)`, so telemetry cannot
be attributed to another tenant's service.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SpanKind(enum.IntEnum):
    """OTLP `Span.SpanKind` values (stored as-is)."""

    UNSPECIFIED = 0
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class SpanStatus(enum.IntEnum):
    """OTLP `Status.StatusCode` values."""

    UNSET = 0
    OK = 1
    ERROR = 2


class MetricKind(enum.IntEnum):
    GAUGE = 1
    SUM = 2
    HISTOGRAM = 3


# Tables partitioned by day, with the column they are partitioned on.
PARTITIONED_TABLES: dict[str, str] = {
    "spans": "start_time",
    "log_records": "ts",
    "metric_points": "ts",
}


def _service_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "service_id"],
        ["services.org_id", "services.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_service",
    )


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "trace_id", "span_id", "start_time", name="pk_spans"),
        _service_fk("spans"),
        CheckConstraint("octet_length(trace_id) = 16", name="trace_id_length"),
        CheckConstraint("octet_length(span_id) = 8", name="span_id_length"),
        CheckConstraint(
            "parent_span_id IS NULL OR octet_length(parent_span_id) = 8",
            name="parent_span_id_length",
        ),
        CheckConstraint("kind BETWEEN 0 AND 5", name="kind_valid"),
        CheckConstraint("status_code BETWEEN 0 AND 2", name="status_code_valid"),
        # Rollups: per-service request/error/latency windows over SERVER spans (T7).
        Index(
            "ix_spans_server_window",
            "org_id",
            "service_id",
            "start_time",
            postgresql_where=text("kind = 2"),
        ),
        # Detection evidence: error spans of a service in a window (T7/T8).
        Index(
            "ix_spans_error_window",
            "org_id",
            "service_id",
            "start_time",
            postgresql_where=text("status_code = 2"),
        ),
        # Dependency graph: a service's database client spans in a window (T6 externals).
        Index(
            "ix_spans_db_client_window",
            "org_id",
            "service_id",
            "start_time",
            postgresql_where=text("kind = 3 AND db_system IS NOT NULL"),
        ),
        # Trace lookups use the primary key prefix (org_id, trace_id).
        {"postgresql_partition_by": "RANGE (start_time)"},
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trace_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    span_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_span_id: Mapped[bytes | None] = mapped_column(LargeBinary)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Double, nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status_message: Mapped[str | None] = mapped_column(String(1024))
    http_method: Mapped[str | None] = mapped_column(String(16))
    http_route: Mapped[str | None] = mapped_column(String(256))
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    db_system: Mapped[str | None] = mapped_column(String(64))
    server_address: Mapped[str | None] = mapped_column(String(256))
    peer_service: Mapped[str | None] = mapped_column(String(128))
    exception_type: Mapped[str | None] = mapped_column(String(256))
    exception_message: Mapped[str | None] = mapped_column(String(2048))
    service_instance_id: Mapped[str | None] = mapped_column(String(128))
    service_version: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class LogRecord(Base):
    __tablename__ = "log_records"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "record_hash", "ts", name="pk_log_records"),
        _service_fk("log_records"),
        CheckConstraint("octet_length(record_hash) = 32", name="record_hash_length"),
        CheckConstraint("trace_id IS NULL OR octet_length(trace_id) = 16", name="trace_id_length"),
        CheckConstraint("span_id IS NULL OR octet_length(span_id) = 8", name="span_id_length"),
        # Logs of a service in a window (detection evidence, exception signatures).
        Index("ix_log_records_service_window", "org_id", "service_id", "ts"),
        # Logs of a trace (incident detail; Phase 4 evidence).
        Index(
            "ix_log_records_trace",
            "org_id",
            "trace_id",
            postgresql_where=text("trace_id IS NOT NULL"),
        ),
        {"postgresql_partition_by": "RANGE (ts)"},
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # SHA-256 over (service, ts, severity, body, trace/span id): identical re-sends collapse.
    record_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    observed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    severity_text: Mapped[str | None] = mapped_column(String(32))
    body: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[bytes | None] = mapped_column(LargeBinary)
    span_id: Mapped[bytes | None] = mapped_column(LargeBinary)
    event_name: Mapped[str | None] = mapped_column(String(128))
    exception_type: Mapped[str | None] = mapped_column(String(256))
    exception_message: Mapped[str | None] = mapped_column(String(2048))
    exception_stacktrace: Mapped[str | None] = mapped_column(Text)
    service_instance_id: Mapped[str | None] = mapped_column(String(128))
    service_version: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class MetricSeries(Base):
    """One time series: service + metric name + kind + attribute set (incl. instance id)."""

    __tablename__ = "metric_series"
    __table_args__ = (
        _service_fk("metric_series"),
        UniqueConstraint("org_id", "series_hash", name="uq_metric_series_org_id_series_hash"),
        UniqueConstraint("org_id", "id", name="uq_metric_series_org_id_id"),
        CheckConstraint("octet_length(series_hash) = 32", name="series_hash_length"),
        CheckConstraint("kind BETWEEN 1 AND 3", name="kind_valid"),
        CheckConstraint("temporality BETWEEN 0 AND 2", name="temporality_valid"),
        # Saturation rules: series of a service by metric name (T7).
        Index("ix_metric_series_service_name", "org_id", "service_id", "name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    series_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_monotonic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # OTLP AggregationTemporality: 0 unspecified, 1 delta, 2 cumulative (decides rate math).
    temporality: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MetricPoint(Base):
    """A data point. Gauge/sum use `value`; histograms use count/sum/buckets.

    Cumulative counters are stored as sent; rates are derived in SQL with resets detected via
    `start_ts` changes (no in-memory state at ingest).
    """

    __tablename__ = "metric_points"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "series_id", "ts", name="pk_metric_points"),
        ForeignKeyConstraint(
            ["org_id", "series_id"],
            ["metric_series.org_id", "metric_series.id"],
            ondelete="CASCADE",
            name="fk_metric_points_series",
        ),
        {"postgresql_partition_by": "RANGE (ts)"},
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    series_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value: Mapped[float | None] = mapped_column(Double)
    count: Mapped[int | None] = mapped_column(BigInteger)
    sum: Mapped[float | None] = mapped_column(Double)
    bucket_counts: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    explicit_bounds: Mapped[list[float] | None] = mapped_column(ARRAY(Double))


class Deployment(Base):
    """A deployment change event, normalized from logs with `event.name = deployment`."""

    __tablename__ = "deployments"
    __table_args__ = (
        _service_fk("deployments"),
        UniqueConstraint(
            "org_id",
            "service_id",
            "deployment_id",
            "status",
            name="uq_deployments_org_id_service_id_deployment_id_status",
        ),
        # Deploy proximity: deployments of a service around a time (T7/T8).
        Index("ix_deployments_service_ts", "org_id", "service_id", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64))
    previous_version: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestUsage(Base):
    """Per-key, per-minute ingest counters: accepted records and rejected records by reason.

    `outcome` is "accepted" or "rejected:<reason>" — the record of "counted and dropped".
    """

    __tablename__ = "ingest_usage"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org_id", "key_id", "signal", "minute", "outcome", name="pk_ingest_usage"
        ),
        _service_fk("ingest_usage"),
        CheckConstraint("signal IN ('traces', 'logs', 'metrics')", name="signal_valid"),
        # Usage of a service over time (service page, Overview).
        Index("ix_ingest_usage_service_minute", "org_id", "service_id", "minute"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_api_keys.id", ondelete="CASCADE", name="fk_ingest_usage_key"),
        nullable=False,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    signal: Mapped[str] = mapped_column(String(8), nullable=False)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False)


class IngestBatch(Base):
    """Batches already written. Recorded in the writer's transaction, so a redelivered queue
    message is skipped entirely (exactly-once effects, including `ingest_usage` counts)."""

    __tablename__ = "ingest_batches"
    __table_args__ = (Index("ix_ingest_batches_processed_at", "processed_at"),)

    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_ingest_batches_org"),
        nullable=False,
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
