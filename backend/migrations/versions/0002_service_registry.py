"""Phase 3 T1: service registry (services, declared dependencies, ingest API keys), project archive.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("environment", sa.String(length=63), nullable=False),
        sa.Column("repo_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "deployment",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "telemetry_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "health_status",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
            "environment ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'",
            name=op.f("ck_services_environment_format"),
        ),
        sa.CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'unhealthy')",
            name=op.f("ck_services_health_status_valid"),
        ),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'", name=op.f("ck_services_name_format")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_services_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_services_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_services_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint("org_id", "id", name="uq_services_org_id_id"),
    )
    op.create_index(
        "ix_services_org_id_created_at_id", "services", ["org_id", "created_at", "id"], unique=False
    )
    op.create_index(
        "ix_services_org_id_project_id", "services", ["org_id", "project_id"], unique=False
    )
    op.create_index(
        "uq_services_active_name",
        "services",
        ["org_id", "project_id", "environment", "name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "service_api_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=12), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "prefix ~ '^[0-9a-f]{12}$'", name=op.f("ck_service_api_keys_prefix_format")
        ),
        sa.CheckConstraint(
            "octet_length(secret_hash) = 32", name=op.f("ck_service_api_keys_secret_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_service_api_keys_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_api_keys_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_service_api_keys_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"],
            ["users.id"],
            name=op.f("fk_service_api_keys_revoked_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_api_keys")),
        sa.UniqueConstraint("prefix", name="uq_service_api_keys_prefix"),
    )
    op.create_index(
        "ix_service_api_keys_org_id_created_at_id",
        "service_api_keys",
        ["org_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_service_api_keys_org_id_service_id",
        "service_api_keys",
        ["org_id", "service_id"],
        unique=False,
    )
    op.create_table(
        "service_dependencies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("target_service_id", sa.UUID(), nullable=True),
        sa.Column("target_external", sa.String(length=255), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_external IS NULL OR target_external ~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$'",
            name=op.f("ck_service_dependencies_external_format"),
        ),
        sa.CheckConstraint(
            "(target_service_id IS NULL) <> (target_external IS NULL)",
            name=op.f("ck_service_dependencies_one_target"),
        ),
        sa.CheckConstraint(
            "target_service_id IS NULL OR target_service_id <> service_id",
            name=op.f("ck_service_dependencies_no_self_edge"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_dependencies_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "target_service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_dependencies_target",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_service_dependencies_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_dependencies")),
    )
    op.create_index(
        "ix_service_dependencies_org_id_target_service_id",
        "service_dependencies",
        ["org_id", "target_service_id"],
        unique=False,
    )
    op.create_index(
        "uq_service_dependencies_target_external",
        "service_dependencies",
        ["org_id", "service_id", "target_external"],
        unique=True,
        postgresql_where=sa.text("target_external IS NOT NULL"),
    )
    op.create_index(
        "uq_service_dependencies_target_service",
        "service_dependencies",
        ["org_id", "service_id", "target_service_id"],
        unique=True,
        postgresql_where=sa.text("target_service_id IS NOT NULL"),
    )
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "archived_at")
    op.drop_index(
        "uq_service_dependencies_target_service",
        table_name="service_dependencies",
        postgresql_where=sa.text("target_service_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_service_dependencies_target_external",
        table_name="service_dependencies",
        postgresql_where=sa.text("target_external IS NOT NULL"),
    )
    op.drop_index(
        "ix_service_dependencies_org_id_target_service_id", table_name="service_dependencies"
    )
    op.drop_table("service_dependencies")
    op.drop_index("ix_service_api_keys_org_id_service_id", table_name="service_api_keys")
    op.drop_index("ix_service_api_keys_org_id_created_at_id", table_name="service_api_keys")
    op.drop_table("service_api_keys")
    op.drop_index(
        "uq_services_active_name",
        table_name="services",
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.drop_index("ix_services_org_id_project_id", table_name="services")
    op.drop_index("ix_services_org_id_created_at_id", table_name="services")
    op.drop_table("services")
