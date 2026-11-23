"""Phase 4 T4: hypotheses, their evidence citations (FK to evidence) and verification checks.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-23 20:07:15.425704+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hypotheses",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("proposed_rank", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("subject_service_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.String(length=200), nullable=False),
        sa.Column("checks_passed", sa.Integer(), nullable=False),
        sa.Column("checks_applicable", sa.Integer(), nullable=False),
        sa.Column("model_confidence", sa.Double(), nullable=False),
        sa.Column(
            "rejected_citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "missing_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT is_primary OR status <> 'inconclusive'",
            name=op.f("ck_hypotheses_primary_not_inconclusive"),
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'probable', 'inconclusive')",
            name=op.f("ck_hypotheses_status_valid"),
        ),
        sa.CheckConstraint(
            "model_confidence BETWEEN 0 AND 1", name=op.f("ck_hypotheses_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            name="fk_hypotheses_investigation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hypotheses")),
        sa.UniqueConstraint(
            "investigation_id", "proposed_rank", name="uq_hypotheses_investigation_proposed"
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_hypotheses_org_id_id"),
    )
    op.create_index(
        "ix_hypotheses_org_id_investigation_id_rank",
        "hypotheses",
        ["org_id", "investigation_id", "rank"],
        unique=False,
    )
    op.create_index(
        "uq_hypotheses_primary",
        "hypotheses",
        ["investigation_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_table(
        "hypothesis_evidence",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("hypothesis_id", sa.UUID(), nullable=False),
        sa.Column("evidence_id", sa.String(length=15), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "role IN ('supporting', 'contradicting')",
            name=op.f("ck_hypothesis_evidence_role_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "hypothesis_id"],
            ["hypotheses.org_id", "hypotheses.id"],
            name="fk_hypothesis_evidence_hypothesis",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "investigation_id", "evidence_id"],
            ["evidence.org_id", "evidence.investigation_id", "evidence.id"],
            name="fk_hypothesis_evidence_evidence",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "hypothesis_id", "evidence_id", "role", name="pk_hypothesis_evidence"
        ),
    )
    op.create_index(
        "ix_hypothesis_evidence_org_id_evidence_id",
        "hypothesis_evidence",
        ["org_id", "evidence_id"],
        unique=False,
    )
    op.create_table(
        "verification_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("hypothesis_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "result IN ('pass', 'fail', 'not_run', 'not_applicable')",
            name=op.f("ck_verification_checks_result_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "hypothesis_id"],
            ["hypotheses.org_id", "hypotheses.id"],
            name="fk_verification_checks_hypothesis",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_checks")),
    )
    op.create_index(
        "ix_verification_checks_org_id_hypothesis_id",
        "verification_checks",
        ["org_id", "hypothesis_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_verification_checks_org_id_hypothesis_id", table_name="verification_checks")
    op.drop_table("verification_checks")
    op.drop_index("ix_hypothesis_evidence_org_id_evidence_id", table_name="hypothesis_evidence")
    op.drop_table("hypothesis_evidence")
    op.drop_index(
        "uq_hypotheses_primary", table_name="hypotheses", postgresql_where=sa.text("is_primary")
    )
    op.drop_index("ix_hypotheses_org_id_investigation_id_rank", table_name="hypotheses")
    op.drop_table("hypotheses")
