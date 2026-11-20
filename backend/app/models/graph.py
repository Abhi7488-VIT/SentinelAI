"""Dependency graph edges (Phase 3 T6).

An edge exists only with evidence, enforced by the database:

- `declared`: points at the `service_dependencies` row a user saved (`declared_dependency_id`),
  and disappears with it (FK cascade).
- `observed`: carries a sample trace id in which the call was seen (`sample_trace_id`).

Both ends reach their tenant's rows through composite FKs, so an edge cannot cross tenants. One
logical connection can have two rows, one per source; the API returns both.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.service import EXTERNAL_TARGET_PATTERN


class EdgeSource(enum.StrEnum):
    DECLARED = "declared"
    OBSERVED = "observed"


class ServiceEdge(Base):
    __tablename__ = "service_edges"
    __table_args__ = (
        CheckConstraint("source IN ('declared', 'observed')", name="source_valid"),
        CheckConstraint(
            "(target_service_id IS NULL) <> (target_external IS NULL)", name="one_target"
        ),
        CheckConstraint(
            "target_service_id IS NULL OR target_service_id <> source_service_id",
            name="no_self_edge",
        ),
        CheckConstraint(
            f"target_external IS NULL OR target_external ~ '{EXTERNAL_TARGET_PATTERN}'",
            name="external_format",
        ),
        # No edge without evidence: exactly the evidence column of its source is set.
        CheckConstraint(
            "(source = 'declared' AND declared_dependency_id IS NOT NULL"
            " AND sample_trace_id IS NULL)"
            " OR (source = 'observed' AND sample_trace_id IS NOT NULL"
            " AND declared_dependency_id IS NULL)",
            name="evidence_required",
        ),
        CheckConstraint(
            "sample_trace_id IS NULL OR octet_length(sample_trace_id) = 16",
            name="sample_trace_id_length",
        ),
        CheckConstraint("call_count >= 0", name="call_count_non_negative"),
        ForeignKeyConstraint(
            ["org_id", "source_service_id"],
            ["services.org_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_edges_source",
        ),
        ForeignKeyConstraint(
            ["org_id", "target_service_id"],
            ["services.org_id", "services.id"],
            ondelete="CASCADE",
            name="fk_service_edges_target",
        ),
        ForeignKeyConstraint(
            ["org_id", "declared_dependency_id"],
            ["service_dependencies.org_id", "service_dependencies.id"],
            ondelete="CASCADE",
            name="fk_service_edges_declared_dependency",
        ),
        Index(
            "uq_service_edges_target_service",
            "org_id",
            "source_service_id",
            "target_service_id",
            "source",
            unique=True,
            postgresql_where=text("target_service_id IS NOT NULL"),
        ),
        Index(
            "uq_service_edges_target_external",
            "org_id",
            "source_service_id",
            "target_external",
            "source",
            unique=True,
            postgresql_where=text("target_external IS NOT NULL"),
        ),
        # Edges into a service (correlation's "dependency_edge" reason, T8).
        Index("ix_service_edges_org_id_target_service_id", "org_id", "target_service_id"),
        # Observed-edge expiry.
        Index(
            "ix_service_edges_observed_last_seen",
            "last_seen_at",
            postgresql_where=text("source = 'observed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_service_edges_org"),
        nullable=False,
    )
    source_service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_external: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Observed: calls counted in the discovery window that last saw this edge (not a lifetime
    # total: windows overlap, so summing them would double count). Declared: 0.
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    sample_trace_id: Mapped[bytes | None] = mapped_column(LargeBinary)
    declared_dependency_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
