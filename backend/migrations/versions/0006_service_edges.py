"""Phase 3 T6: dependency graph edges (declared + observed) and the DB-client span index.

Declared edges are backfilled from existing `service_dependencies` rows.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTERNAL = "^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_service_dependencies_org_id_id", "service_dependencies", ["org_id", "id"]
    )
    op.create_table(
        "service_edges",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("source_service_id", sa.UUID(), nullable=False),
        sa.Column("target_service_id", sa.UUID(), nullable=True),
        sa.Column("target_external", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sample_trace_id", sa.LargeBinary(), nullable=True),
        sa.Column("declared_dependency_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "source IN ('declared', 'observed')", name=op.f("ck_service_edges_source_valid")
        ),
        sa.CheckConstraint(
            "(target_service_id IS NULL) <> (target_external IS NULL)",
            name=op.f("ck_service_edges_one_target"),
        ),
        sa.CheckConstraint(
            "target_service_id IS NULL OR target_service_id <> source_service_id",
            name=op.f("ck_service_edges_no_self_edge"),
        ),
        sa.CheckConstraint(
            f"target_external IS NULL OR target_external ~ '{_EXTERNAL}'",
            name=op.f("ck_service_edges_external_format"),
        ),
        sa.CheckConstraint(
            "(source = 'declared' AND declared_dependency_id IS NOT NULL"
            " AND sample_trace_id IS NULL)"
            " OR (source = 'observed' AND sample_trace_id IS NOT NULL"
            " AND declared_dependency_id IS NULL)",
            name=op.f("ck_service_edges_evidence_required"),
        ),
        sa.CheckConstraint(
            "sample_trace_id IS NULL OR octet_length(sample_trace_id) = 16",
            name=op.f("ck_service_edges_sample_trace_id_length"),
        ),
        sa.CheckConstraint(
            "call_count >= 0", name=op.f("ck_service_edges_call_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_edges_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "target_service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_edges_target",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "declared_dependency_id"],
            ["service_dependencies.org_id", "service_dependencies.id"],
            name="fk_service_edges_declared_dependency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_service_edges_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_edges")),
    )
    op.create_index(
        "uq_service_edges_target_service",
        "service_edges",
        ["org_id", "source_service_id", "target_service_id", "source"],
        unique=True,
        postgresql_where=sa.text("target_service_id IS NOT NULL"),
    )
    op.create_index(
        "uq_service_edges_target_external",
        "service_edges",
        ["org_id", "source_service_id", "target_external", "source"],
        unique=True,
        postgresql_where=sa.text("target_external IS NOT NULL"),
    )
    op.create_index(
        "ix_service_edges_org_id_target_service_id",
        "service_edges",
        ["org_id", "target_service_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_edges_observed_last_seen",
        "service_edges",
        ["last_seen_at"],
        unique=False,
        postgresql_where=sa.text("source = 'observed'"),
    )
    # Created on the partitioned parent: Postgres builds it on every existing partition and on
    # partitions created later.
    op.create_index(
        "ix_spans_db_client_window",
        "spans",
        ["org_id", "service_id", "start_time"],
        unique=False,
        postgresql_where=sa.text("kind = 3 AND db_system IS NOT NULL"),
    )
    op.execute(
        """
        INSERT INTO service_edges (org_id, source_service_id, target_service_id, target_external,
                                   source, first_seen_at, last_seen_at, declared_dependency_id)
        SELECT org_id, service_id, target_service_id, target_external,
               'declared', created_at, created_at, id
        FROM service_dependencies
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spans_db_client_window",
        table_name="spans",
        postgresql_where=sa.text("kind = 3 AND db_system IS NOT NULL"),
    )
    op.drop_index(
        "ix_service_edges_observed_last_seen",
        table_name="service_edges",
        postgresql_where=sa.text("source = 'observed'"),
    )
    op.drop_index("ix_service_edges_org_id_target_service_id", table_name="service_edges")
    op.drop_index(
        "uq_service_edges_target_external",
        table_name="service_edges",
        postgresql_where=sa.text("target_external IS NOT NULL"),
    )
    op.drop_index(
        "uq_service_edges_target_service",
        table_name="service_edges",
        postgresql_where=sa.text("target_service_id IS NOT NULL"),
    )
    op.drop_table("service_edges")
    op.drop_constraint("uq_service_dependencies_org_id_id", "service_dependencies", type_="unique")
