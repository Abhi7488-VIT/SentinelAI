"""Investigations and their evidence (Phase 4 plan §4, §9).

T2 created the `investigations` row (identity, tenant, incident, status); T3 adds the lease (one
worker runs an investigation at a time) and lifecycle columns; T5 adds budgets and cancellation.

Evidence is created only by deterministic tools (`app/investigation/tools/`), never by an LLM
and never through the API. `(investigation_id, dedup_key)` is unique, so a re-run tool (crash
resume, task retry) gets the existing row back instead of a duplicate (invariant 6). Both tables
reach their tenant through composite FKs, so evidence cannot be attached across orgs.
"""

from __future__ import annotations

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
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

INVESTIGATION_STATUSES = ("queued", "running", "completed", "incomplete", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running")
EVIDENCE_KINDS = (
    "log_query",
    "metric_query",
    "trace_analysis",
    "deploy_lookup",
    "dependency_lookup",
    "detection_ref",
)
# Hard cap on `evidence.data` (bytes of its JSON text). The Python side trims to a smaller
# budget (`app.investigation.evidence.DATA_BUDGET_BYTES`) so jsonb's own formatting never trips it.
HYPOTHESIS_STATUSES = ("confirmed", "probable", "inconclusive")
CHECK_RESULTS = ("pass", "fail", "not_run", "not_applicable")
EVIDENCE_DATA_MAX_BYTES = 16 * 1024
EVIDENCE_SUMMARY_MAX_CHARS = 1000
# `ev_` + 12 base32 characters.
EVIDENCE_ID_PATTERN = r"^ev_[a-z2-7]{12}$"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Investigation(TimestampMixin, Base):
    __tablename__ = "investigations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "incident_id"],
            ["incidents.org_id", "incidents.id"],
            ondelete="CASCADE",
            name="fk_investigations_incident",
        ),
        UniqueConstraint("org_id", "id", name="uq_investigations_org_id_id"),
        CheckConstraint(_in("status", INVESTIGATION_STATUSES), name="status_valid"),
        # One active investigation per incident: a repeated start returns the existing one.
        Index(
            "uq_investigations_active_incident",
            "org_id",
            "incident_id",
            unique=True,
            postgresql_where=text(f"{_in('status', ACTIVE_STATUSES)}"),
        ),
        Index("ix_investigations_org_id_created_at_id", "org_id", "created_at", "id"),
        Index("ix_investigations_org_id_incident_id", "org_id", "incident_id"),
        CheckConstraint("(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="lease_pair"),
        # Expired-lease sweep (T5): running investigations by lease expiry.
        Index(
            "ix_investigations_running_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_investigations_org"),
        nullable=False,
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Exactly what is missing when the run ends `incomplete` (e.g. `no_telemetry:orders:logs`).
    incomplete_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lease: the worker that runs it, until when (renewed while running; plan §7 crash resume).
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            ondelete="CASCADE",
            name="fk_evidence_investigation",
        ),
        UniqueConstraint("org_id", "id", name="uq_evidence_org_id_id"),
        # Target of hypothesis citations (T4): a citation names the investigation too.
        UniqueConstraint(
            "org_id", "investigation_id", "id", name="uq_evidence_org_id_investigation_id_id"
        ),
        UniqueConstraint("investigation_id", "dedup_key", name="uq_evidence_dedup"),
        CheckConstraint(f"id ~ '{EVIDENCE_ID_PATTERN}'", name="id_format"),
        CheckConstraint(_in("kind", EVIDENCE_KINDS), name="kind_valid"),
        CheckConstraint("time_from < time_to", name="window_ordered"),
        CheckConstraint(
            f"char_length(summary) <= {EVIDENCE_SUMMARY_MAX_CHARS}", name="summary_length"
        ),
        CheckConstraint(f"octet_length(data::text) <= {EVIDENCE_DATA_MAX_BYTES}", name="data_size"),
        CheckConstraint("row_count >= 0", name="row_count_non_negative"),
        CheckConstraint("octet_length(dedup_key) = 64", name="dedup_key_length"),
        # Evidence browser: an investigation's evidence by kind, newest first.
        Index(
            "ix_evidence_org_id_investigation_id_kind_created_at",
            "org_id",
            "investigation_id",
            "kind",
            "created_at",
        ),
        Index(
            "ix_evidence_org_id_investigation_id_created_at_id",
            "org_id",
            "investigation_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(15), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # The exact, re-runnable query: tool name and normalised parameters.
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Services the evidence is about (evidence browser filter; may be empty for detection_ref).
    service_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    time_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(String(EVIDENCE_SUMMARY_MAX_CHARS), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_node: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Hypothesis(Base):
    """A root-cause candidate proposed by the analyst; `status`, `rank` and `is_primary` are set
    only by deterministic verification (T4). `model_confidence` is for display only."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            ondelete="CASCADE",
            name="fk_hypotheses_investigation",
        ),
        UniqueConstraint("org_id", "id", name="uq_hypotheses_org_id_id"),
        # Re-running verification replaces, never duplicates (invariant 6).
        UniqueConstraint(
            "investigation_id", "proposed_rank", name="uq_hypotheses_investigation_proposed"
        ),
        CheckConstraint(_in("status", HYPOTHESIS_STATUSES), name="status_valid"),
        CheckConstraint("model_confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint(
            "NOT is_primary OR status <> 'inconclusive'", name="primary_not_inconclusive"
        ),
        Index("ix_hypotheses_org_id_investigation_id_rank", "org_id", "investigation_id", "rank"),
        # At most one primary per investigation.
        Index(
            "uq_hypotheses_primary",
            "investigation_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposed_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_service_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_reason: Mapped[str] = mapped_column(String(200), nullable=False)
    checks_passed: Mapped[int] = mapped_column(Integer, nullable=False)
    checks_applicable: Mapped[int] = mapped_column(Integer, nullable=False)
    model_confidence: Mapped[float] = mapped_column(Double, nullable=False)
    rejected_citations: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    missing_evidence: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HypothesisEvidence(Base):
    """A citation. The composite FK to `evidence (org_id, investigation_id, id)` makes a dangling
    or cross-investigation citation impossible at the database level (invariant 1)."""

    __tablename__ = "hypothesis_evidence"
    __table_args__ = (
        PrimaryKeyConstraint("hypothesis_id", "evidence_id", "role", name="pk_hypothesis_evidence"),
        ForeignKeyConstraint(
            ["org_id", "hypothesis_id"],
            ["hypotheses.org_id", "hypotheses.id"],
            ondelete="CASCADE",
            name="fk_hypothesis_evidence_hypothesis",
        ),
        ForeignKeyConstraint(
            ["org_id", "investigation_id", "evidence_id"],
            ["evidence.org_id", "evidence.investigation_id", "evidence.id"],
            ondelete="CASCADE",
            name="fk_hypothesis_evidence_evidence",
        ),
        CheckConstraint("role IN ('supporting', 'contradicting')", name="role_valid"),
        Index("ix_hypothesis_evidence_org_id_evidence_id", "org_id", "evidence_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(15), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class VerificationCheck(Base):
    """One deterministic check of a hypothesis and the evidence ids it looked at."""

    __tablename__ = "verification_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "hypothesis_id"],
            ["hypotheses.org_id", "hypotheses.id"],
            ondelete="CASCADE",
            name="fk_verification_checks_hypothesis",
        ),
        CheckConstraint(_in("result", CHECK_RESULTS), name="result_valid"),
        Index("ix_verification_checks_org_id_hypothesis_id", "org_id", "hypothesis_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
