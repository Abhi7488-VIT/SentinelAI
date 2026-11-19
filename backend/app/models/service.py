"""Service registry: services, their declared dependencies, and per-service ingest API keys.

All three tables are tenant-owned (org_id) and reach their parents through composite foreign keys
``(org_id, <parent>_id) -> parent(org_id, id)``, so a row can never point into another tenant
(ADR 0002 §5).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# OpenTelemetry `service.name` values we accept: no spaces or control characters.
SERVICE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
ENVIRONMENT_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
# External dependency identifiers, e.g. "postgresql:orders" or "redis:sessions".
EXTERNAL_TARGET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$"


class HealthStatus(enum.StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


_HEALTH_VALUES = ", ".join(f"'{h.value}'" for h in HealthStatus)


class Service(TimestampMixin, Base):
    """A monitored service. `name` must equal the OTel resource attribute `service.name`."""

    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint(f"name ~ '{SERVICE_NAME_PATTERN}'", name="name_format"),
        CheckConstraint(f"environment ~ '{ENVIRONMENT_PATTERN}'", name="environment_format"),
        CheckConstraint(f"health_status IN ({_HEALTH_VALUES})", name="health_status_valid"),
        ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_services_project",
        ),
        UniqueConstraint("org_id", "id", name="uq_services_org_id_id"),
        # One active service per (project, environment, name); archived ones free the name.
        Index(
            "uq_services_active_name",
            "org_id",
            "project_id",
            "environment",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_services_org_id_created_at_id", "org_id", "created_at", "id"),
        Index("ix_services_org_id_project_id", "org_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_services_org"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    environment: Mapped[str] = mapped_column(String(63), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(2048))
    deployment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    telemetry_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Written by detection only (Phase 3 T7); never by clients.
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text(f"'{HealthStatus.UNKNOWN.value}'")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class ServiceDependency(Base):
    """A dependency declared by a user: service -> another service, or -> an external system.

    The only source of `declared` edges in the dependency graph (Phase 3 T6).
    """

    __tablename__ = "service_dependencies"
    __table_args__ = (
        CheckConstraint(
            "(target_service_id IS NULL) <> (target_external IS NULL)", name="one_target"
        ),
        CheckConstraint(
            "target_service_id IS NULL OR target_service_id <> service_id", name="no_self_edge"
        ),
        CheckConstraint(
            f"target_external IS NULL OR target_external ~ '{EXTERNAL_TARGET_PATTERN}'",
            name="external_format",
        ),
        ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_dependencies_service",
        ),
        ForeignKeyConstraint(
            ["org_id", "target_service_id"],
            ["services.org_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_dependencies_target",
        ),
        Index(
            "uq_service_dependencies_target_service",
            "org_id",
            "service_id",
            "target_service_id",
            unique=True,
            postgresql_where=text("target_service_id IS NOT NULL"),
        ),
        Index(
            "uq_service_dependencies_target_external",
            "org_id",
            "service_id",
            "target_external",
            unique=True,
            postgresql_where=text("target_external IS NOT NULL"),
        ),
        Index("ix_service_dependencies_org_id_target_service_id", "org_id", "target_service_id"),
        # Target of the composite FK from service_edges (declared edges cite their dependency).
        UniqueConstraint("org_id", "id", name="uq_service_dependencies_org_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_service_dependencies_org"),
        nullable=False,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_external: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServiceApiKey(Base):
    """An ingest key bound to one service. Only a SHA-256 of the token is stored."""

    __tablename__ = "service_api_keys"
    __table_args__ = (
        CheckConstraint("prefix ~ '^[0-9a-f]{12}$'", name="prefix_format"),
        CheckConstraint("octet_length(secret_hash) = 32", name="secret_hash_length"),
        ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_api_keys_service",
        ),
        UniqueConstraint("prefix", name="uq_service_api_keys_prefix"),
        Index("ix_service_api_keys_org_id_created_at_id", "org_id", "created_at", "id"),
        Index("ix_service_api_keys_org_id_service_id", "org_id", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_service_api_keys_org"),
        nullable=False,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
