"""Phase 3 T7: detection storage (window stats, instances, detector states, detections).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RULES = "'error_rate', 'latency_shift', 'resource_saturation', 'health_check', 'deploy_change'"


def upgrade() -> None:
    op.create_table(
        "detections",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("observed_value", sa.Double(), nullable=True),
        sa.Column("baseline_value", sa.Double(), nullable=True),
        sa.Column("threshold", sa.Double(), nullable=True),
        sa.Column("peak_value", sa.Double(), nullable=True),
        sa.Column("last_value", sa.Double(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"rule IN ({_RULES})",
            name=op.f("ck_detections_rule_valid"),
        ),
        sa.CheckConstraint(
            "severity IN ('warning', 'critical')", name=op.f("ck_detections_severity_valid")
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= fired_at",
            name=op.f("ck_detections_resolved_after"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_detections_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_detections_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detections")),
        sa.UniqueConstraint("org_id", "id", name="uq_detections_org_id_id"),
        sa.UniqueConstraint(
            "org_id",
            "service_id",
            "rule",
            "started_at",
            name="uq_detections_org_id_service_id_rule_started_at",
        ),
    )
    op.create_index(
        "ix_detections_open",
        "detections",
        ["org_id", "service_id"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        "ix_detections_org_id_created_at_id",
        "detections",
        ["org_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_detections_org_id_service_id_started_at",
        "detections",
        ["org_id", "service_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "service_instances",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_instances_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "service_id", "instance_id", name="pk_service_instances"),
    )
    op.create_index(
        "ix_service_instances_last_seen_at", "service_instances", ["last_seen_at"], unique=False
    )
    op.create_index(
        "ix_service_instances_org_id_service_id_first_seen_at",
        "service_instances",
        ["org_id", "service_id", "first_seen_at"],
        unique=False,
    )
    op.create_table(
        "service_window_stats",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("p50_ms", sa.Double(), nullable=True),
        sa.Column("p95_ms", sa.Double(), nullable=True),
        sa.Column("p99_ms", sa.Double(), nullable=True),
        sa.Column("requests_60s", sa.Integer(), nullable=False),
        sa.Column("errors_60s", sa.Integer(), nullable=False),
        sa.Column("p95_60s_ms", sa.Double(), nullable=True),
        sa.Column(
            "exceptions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "requests >= 0 AND errors >= 0 AND errors <= requests",
            name=op.f("ck_service_window_stats_counts"),
        ),
        sa.CheckConstraint(
            "requests_60s >= 0 AND errors_60s >= 0 AND errors_60s <= requests_60s",
            name=op.f("ck_service_window_stats_counts_60s"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_service_window_stats_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "service_id", "bucket_start", name="pk_service_window_stats"
        ),
    )
    op.create_index(
        "ix_service_window_stats_bucket_start",
        "service_window_stats",
        ["bucket_start"],
        unique=False,
    )
    op.create_table(
        "detector_states",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_cond", sa.String(length=16), nullable=True),
        sa.Column("last_value", sa.Double(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_detection_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"rule IN ({_RULES})",
            name=op.f("ck_detector_states_rule_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('ok', 'pending', 'firing', 'resolving', 'warming_up')",
            name=op.f("ck_detector_states_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "open_detection_id"],
            ["detections.org_id", "detections.id"],
            name="fk_detector_states_open_detection",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_detector_states_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detector_states")),
        sa.UniqueConstraint(
            "org_id", "service_id", "rule", name="uq_detector_states_org_id_service_id_rule"
        ),
    )
    op.create_index(
        "ix_detector_states_org_id_created_at_id",
        "detector_states",
        ["org_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_detector_states_org_id_created_at_id", table_name="detector_states")
    op.drop_table("detector_states")
    op.drop_index("ix_service_window_stats_bucket_start", table_name="service_window_stats")
    op.drop_table("service_window_stats")
    op.drop_index(
        "ix_service_instances_org_id_service_id_first_seen_at", table_name="service_instances"
    )
    op.drop_index("ix_service_instances_last_seen_at", table_name="service_instances")
    op.drop_table("service_instances")
    op.drop_index("ix_detections_org_id_service_id_started_at", table_name="detections")
    op.drop_index("ix_detections_org_id_created_at_id", table_name="detections")
    op.drop_index(
        "ix_detections_open",
        table_name="detections",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.drop_table("detections")
