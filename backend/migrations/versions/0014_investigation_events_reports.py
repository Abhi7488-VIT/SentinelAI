"""Phase 4 T7: investigation events (SSE, monotonic ids) and reports (citation-gated).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-23 20:44:01.359317+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("once_key", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ("
            + ", ".join(
                [
                    "'stage_started'",
                    "'stage_completed'",
                    "'tool_completed'",
                    "'llm_call'",
                    "'hypothesis_added'",
                    "'hypothesis_verified'",
                    "'budget_warning'",
                    "'rejected_by_allowlist'",
                    "'status_changed'",
                    "'report_ready'",
                ]
            )
            + ")",
            name=op.f("ck_investigation_events_type_valid"),
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 4096", name=op.f("ck_investigation_events_payload_size")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            name="fk_investigation_events_investigation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_events")),
    )
    op.create_index(
        "ix_investigation_events_org_id_investigation_id_id",
        "investigation_events",
        ["org_id", "investigation_id", "id"],
        unique=False,
    )
    op.create_index(
        "uq_investigation_events_once",
        "investigation_events",
        ["investigation_id", "once_key"],
        unique=True,
        postgresql_where=sa.text("once_key IS NOT NULL"),
    )
    op.create_table(
        "investigation_reports",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cited_evidence_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            name="fk_investigation_reports_investigation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_reports")),
        sa.UniqueConstraint("investigation_id", name="uq_investigation_reports_investigation"),
    )
    op.create_index(
        "ix_investigation_reports_org_id_investigation_id",
        "investigation_reports",
        ["org_id", "investigation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investigation_reports_org_id_investigation_id", table_name="investigation_reports"
    )
    op.drop_table("investigation_reports")
    op.drop_index(
        "uq_investigation_events_once",
        table_name="investigation_events",
        postgresql_where=sa.text("once_key IS NOT NULL"),
    )
    op.drop_index(
        "ix_investigation_events_org_id_investigation_id_id", table_name="investigation_events"
    )
    op.drop_table("investigation_events")
