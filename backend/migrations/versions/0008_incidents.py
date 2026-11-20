"""Phase 3 T8: incidents, incident detections/services/events; pg_trgm for title search.

The pg_trgm extension (trusted, ships with postgres:17) is left installed on downgrade.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "incidents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detections_resolved_at", sa.DateTime(timezone=True), nullable=True),
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
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name=op.f("ck_incidents_resolved_at_matches"),
        ),
        sa.CheckConstraint(
            "severity IN ('warning', 'critical')", name=op.f("ck_incidents_severity_valid")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'investigating', 'mitigated', 'resolved')",
            name=op.f("ck_incidents_status_valid"),
        ),
        sa.CheckConstraint("number > 0", name=op.f("ck_incidents_number_positive")),
        sa.ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_incidents_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_incidents_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
        sa.UniqueConstraint("org_id", "id", name="uq_incidents_org_id_id"),
        sa.UniqueConstraint("org_id", "number", name="uq_incidents_org_id_number"),
    )
    op.create_index(
        "ix_incidents_active",
        "incidents",
        ["org_id", "project_id", "last_activity_at"],
        unique=False,
        postgresql_where=sa.text("status <> 'resolved'"),
    )
    op.create_index(
        "ix_incidents_org_id_created_at_id",
        "incidents",
        ["org_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_incidents_org_id_started_at", "incidents", ["org_id", "started_at"], unique=False
    )
    op.create_index(
        "ix_incidents_org_id_status_created_at_id",
        "incidents",
        ["org_id", "status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_incidents_title_trgm",
        "incidents",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_table(
        "incident_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=8), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor = 'user' OR actor_user_id IS NULL",
            name=op.f("ck_incident_events_actor_user_matches"),
        ),
        sa.CheckConstraint(
            "actor IN ('system', 'user')", name=op.f("ck_incident_events_actor_valid")
        ),
        sa.CheckConstraint(
            "type IN ('created', 'detection_added', 'severity_changed', 'status_changed', "
            "'all_detections_resolved')",
            name=op.f("ck_incident_events_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_incident_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "incident_id"],
            ["incidents.org_id", "incidents.id"],
            name="fk_incident_events_incident",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident_events")),
    )
    op.create_index(
        "ix_incident_events_org_id_incident_id_id",
        "incident_events",
        ["org_id", "incident_id", "id"],
        unique=False,
    )
    op.create_table(
        "incident_services",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), server_default="affected", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role = 'affected'", name=op.f("ck_incident_services_role_valid")),
        sa.ForeignKeyConstraint(
            ["org_id", "incident_id"],
            ["incidents.org_id", "incidents.id"],
            name="fk_incident_services_incident",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_incident_services_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "incident_id", "service_id", name="pk_incident_services"),
    )
    op.create_index(
        "ix_incident_services_org_id_service_id",
        "incident_services",
        ["org_id", "service_id", "incident_id"],
        unique=False,
    )
    op.create_table(
        "incident_detections",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("detection_id", sa.UUID(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "detection_id"],
            ["detections.org_id", "detections.id"],
            name="fk_incident_detections_detection",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "incident_id"],
            ["incidents.org_id", "incidents.id"],
            name="fk_incident_detections_incident",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "detection_id", name="pk_incident_detections"),
    )
    op.create_index(
        "ix_incident_detections_org_id_incident_id",
        "incident_detections",
        ["org_id", "incident_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_incident_detections_org_id_incident_id", table_name="incident_detections")
    op.drop_table("incident_detections")
    op.drop_index("ix_incident_services_org_id_service_id", table_name="incident_services")
    op.drop_table("incident_services")
    op.drop_index("ix_incident_events_org_id_incident_id_id", table_name="incident_events")
    op.drop_table("incident_events")
    op.drop_index(
        "ix_incidents_title_trgm",
        table_name="incidents",
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.drop_index("ix_incidents_org_id_status_created_at_id", table_name="incidents")
    op.drop_index("ix_incidents_org_id_started_at", table_name="incidents")
    op.drop_index("ix_incidents_org_id_created_at_id", table_name="incidents")
    op.drop_index(
        "ix_incidents_active",
        table_name="incidents",
        postgresql_where=sa.text("status <> 'resolved'"),
    )
    op.drop_table("incidents")
