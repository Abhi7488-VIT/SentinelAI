"""Phase 4 T2: investigations (identity + status) and evidence (tool output, deduplicated).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-23 19:35:04.520112+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'incomplete', 'failed', 'cancelled')",
            name=op.f("ck_investigations_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_investigations_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "incident_id"],
            ["incidents.org_id", "incidents.id"],
            name="fk_investigations_incident",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_investigations_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
        sa.UniqueConstraint("org_id", "id", name="uq_investigations_org_id_id"),
    )
    op.create_index(
        "ix_investigations_org_id_created_at_id",
        "investigations",
        ["org_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_investigations_org_id_incident_id",
        "investigations",
        ["org_id", "incident_id"],
        unique=False,
    )
    op.create_index(
        "uq_investigations_active_incident",
        "investigations",
        ["org_id", "incident_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=15), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "service_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("time_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("created_by_node", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id ~ '^ev_[a-z2-7]{12}$'", name=op.f("ck_evidence_id_format")),
        sa.CheckConstraint(
            "kind IN ('log_query', 'metric_query', 'trace_analysis', 'deploy_lookup', "
            "'dependency_lookup', 'detection_ref')",
            name=op.f("ck_evidence_kind_valid"),
        ),
        sa.CheckConstraint("char_length(summary) <= 1000", name=op.f("ck_evidence_summary_length")),
        sa.CheckConstraint("octet_length(data::text) <= 16384", name=op.f("ck_evidence_data_size")),
        sa.CheckConstraint(
            "octet_length(dedup_key) = 64", name=op.f("ck_evidence_dedup_key_length")
        ),
        sa.CheckConstraint("row_count >= 0", name=op.f("ck_evidence_row_count_non_negative")),
        sa.CheckConstraint("time_from < time_to", name=op.f("ck_evidence_window_ordered")),
        sa.ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            name="fk_evidence_investigation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.UniqueConstraint("investigation_id", "dedup_key", name="uq_evidence_dedup"),
        sa.UniqueConstraint("org_id", "id", name="uq_evidence_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "investigation_id", "id", name="uq_evidence_org_id_investigation_id_id"
        ),
    )
    op.create_index(
        "ix_evidence_org_id_investigation_id_created_at_id",
        "evidence",
        ["org_id", "investigation_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_org_id_investigation_id_kind_created_at",
        "evidence",
        ["org_id", "investigation_id", "kind", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_org_id_investigation_id_kind_created_at", table_name="evidence")
    op.drop_index("ix_evidence_org_id_investigation_id_created_at_id", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index(
        "uq_investigations_active_incident",
        table_name="investigations",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_investigations_org_id_incident_id", table_name="investigations")
    op.drop_index("ix_investigations_org_id_created_at_id", table_name="investigations")
    op.drop_table("investigations")
