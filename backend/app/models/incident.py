"""Incidents (Phase 3 T8): correlated detections, affected services and a timeline.

Fields are derived from detections (title, severity, services, start), never invented; no root
cause is stored in Phase 3. Every change appends an `incident_events` row whose BIGINT identity
id is monotonic (Phase 4 SSE replays from `Last-Event-ID`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

INCIDENT_STATUSES = ("open", "investigating", "mitigated", "resolved")
SEVERITIES = ("warning", "critical")
EVENT_TYPES = (
    "created",
    "detection_added",
    "severity_changed",
    "status_changed",
    "all_detections_resolved",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _incident_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["org_id", "incident_id"],
        ["incidents.org_id", "incidents.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_incident",
    )


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_incidents_project",
        ),
        UniqueConstraint("org_id", "id", name="uq_incidents_org_id_id"),
        UniqueConstraint("org_id", "number", name="uq_incidents_org_id_number"),
        CheckConstraint(_in("status", INCIDENT_STATUSES), name="status_valid"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity_valid"),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)", name="resolved_at_matches"
        ),
        # List: newest first, optionally by status; time-range filter on started_at.
        Index("ix_incidents_org_id_created_at_id", "org_id", "created_at", "id"),
        Index("ix_incidents_org_id_status_created_at_id", "org_id", "status", "created_at", "id"),
        Index("ix_incidents_org_id_started_at", "org_id", "started_at"),
        # Title search (`q`), pg_trgm.
        Index(
            "ix_incidents_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        # Correlation candidates: unresolved incidents of a project.
        Index(
            "ix_incidents_active",
            "org_id",
            "project_id",
            "last_activity_at",
            postgresql_where=text("status <> 'resolved'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_incidents_org"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the `all_detections_resolved` event was written (cleared when a detection joins).
    detections_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentDetection(Base):
    """Why a detection is in an incident: the structural reasons that held when it joined."""

    __tablename__ = "incident_detections"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "detection_id", name="pk_incident_detections"),
        _incident_fk("incident_detections"),
        ForeignKeyConstraint(
            ["org_id", "detection_id"],
            ["detections.org_id", "detections.id"],
            ondelete="CASCADE",
            name="fk_incident_detections_detection",
        ),
        Index("ix_incident_detections_org_id_incident_id", "org_id", "incident_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    detection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # [{type, detail, refs}] — empty for the detection that opened the incident.
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IncidentService(Base):
    __tablename__ = "incident_services"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "incident_id", "service_id", name="pk_incident_services"),
        _incident_fk("incident_services"),
        ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            ondelete="CASCADE",
            name="fk_incident_services_service",
        ),
        CheckConstraint("role = 'affected'", name="role_valid"),
        # Incident list filtered by service.
        Index("ix_incident_services_org_id_service_id", "org_id", "service_id", "incident_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="affected")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    __table_args__ = (
        _incident_fk("incident_events"),
        CheckConstraint(_in("type", EVENT_TYPES), name="type_valid"),
        CheckConstraint("actor IN ('system', 'user')", name="actor_valid"),
        # System events never carry a user (user events keep theirs unless the user is deleted).
        CheckConstraint("actor = 'user' OR actor_user_id IS NULL", name="actor_user_matches"),
        Index("ix_incident_events_org_id_incident_id_id", "org_id", "incident_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(8), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
