"""Detection storage (Phase 3 T7, ADR 0006): rollups, instances, detector states, detections.

State lives in Postgres, not worker memory, so a killed worker resumes where it stopped
(invariant 5); the detections' unique key makes a retried tick unable to duplicate them
(invariant 6).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RULES = ("error_rate", "latency_shift", "resource_saturation", "health_check", "deploy_change")
STATES = ("ok", "pending", "firing", "resolving", "warming_up")
SEVERITIES = ("warning", "critical")
_RULES_SQL = ", ".join(f"'{r}'" for r in RULES)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _service_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "service_id"],
        ["services.org_id", "services.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_service",
    )


class ServiceWindowStats(Base):
    """Server-span stats per service and 15-s bucket, plus the trailing 60 s ending at the
    bucket's end (what the rules evaluate). Recomputed for recent buckets on every tick."""

    __tablename__ = "service_window_stats"
    __table_args__ = (
        PrimaryKeyConstraint(
            "org_id", "service_id", "bucket_start", name="pk_service_window_stats"
        ),
        _service_fk("service_window_stats"),
        CheckConstraint("requests >= 0 AND errors >= 0 AND errors <= requests", name="counts"),
        CheckConstraint(
            "requests_60s >= 0 AND errors_60s >= 0 AND errors_60s <= requests_60s",
            name="counts_60s",
        ),
        Index("ix_service_window_stats_bucket_start", "bucket_start"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requests: Mapped[int] = mapped_column(Integer, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, nullable=False)
    p50_ms: Mapped[float | None] = mapped_column(Double)
    p95_ms: Mapped[float | None] = mapped_column(Double)
    p99_ms: Mapped[float | None] = mapped_column(Double)
    requests_60s: Mapped[int] = mapped_column(Integer, nullable=False)
    errors_60s: Mapped[int] = mapped_column(Integer, nullable=False)
    p95_60s_ms: Mapped[float | None] = mapped_column(Double)
    # Top exception signatures in the bucket: [{signature, type, message, count}] (≤ 5).
    exceptions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServiceInstance(Base):
    """`service.instance.id`s seen per service (first/last seen): crash-loop detection."""

    __tablename__ = "service_instances"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "service_id", "instance_id", name="pk_service_instances"),
        _service_fk("service_instances"),
        Index(
            "ix_service_instances_org_id_service_id_first_seen_at",
            "org_id",
            "service_id",
            "first_seen_at",
        ),
        Index("ix_service_instances_last_seen_at", "last_seen_at"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DetectorState(Base):
    """One state machine per (service, rule)."""

    __tablename__ = "detector_states"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "service_id", "rule", name="uq_detector_states_org_id_service_id_rule"
        ),
        _service_fk("detector_states"),
        CheckConstraint(f"rule IN ({_RULES_SQL})", name="rule_valid"),
        CheckConstraint(_in("state", STATES), name="state_valid"),
        ForeignKeyConstraint(
            ["org_id", "open_detection_id"],
            ["detections.org_id", "detections.id"],
            name="fk_detector_states_open_detection",
        ),
        Index("ix_detector_states_org_id_created_at_id", "org_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_cond: Mapped[str | None] = mapped_column(String(16))
    last_value: Mapped[float | None] = mapped_column(Double)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_detection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Detection(Base):
    __tablename__ = "detections"
    __table_args__ = (
        _service_fk("detections"),
        UniqueConstraint("org_id", "id", name="uq_detections_org_id_id"),
        # A retried tick cannot create the same detection twice.
        UniqueConstraint(
            "org_id",
            "service_id",
            "rule",
            "started_at",
            name="uq_detections_org_id_service_id_rule_started_at",
        ),
        CheckConstraint(f"rule IN ({_RULES_SQL})", name="rule_valid"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity_valid"),
        CheckConstraint("resolved_at IS NULL OR resolved_at >= fired_at", name="resolved_after"),
        Index("ix_detections_org_id_created_at_id", "org_id", "created_at", "id"),
        Index("ix_detections_org_id_service_id_started_at", "org_id", "service_id", "started_at"),
        Index(
            "ix_detections_open",
            "org_id",
            "service_id",
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_detections_org"),
        nullable=False,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Double)
    baseline_value: Mapped[float | None] = mapped_column(Double)
    threshold: Mapped[float | None] = mapped_column(Double)
    peak_value: Mapped[float | None] = mapped_column(Double)
    last_value: Mapped[float | None] = mapped_column(Double)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    # Query window, sample counts, ≤ 20 sample trace ids (hex), exception signatures,
    # deployment id / linked detections — everything the explanation used.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "RULES",
    "SEVERITIES",
    "STATES",
    "Detection",
    "DetectorState",
    "ServiceInstance",
    "ServiceWindowStats",
]
