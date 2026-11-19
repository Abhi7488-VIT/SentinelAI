"""Phase 3 T2: telemetry storage (day-partitioned spans/logs/metric points), deployments, usage.

Only partitioned parents are created here; daily partitions are managed at runtime
(app.services.telemetry_maintenance). See ADR 0005.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("deployment_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("previous_version", sa.String(length=64), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_deployments_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint(
            "org_id",
            "service_id",
            "deployment_id",
            "status",
            name="uq_deployments_org_id_service_id_deployment_id_status",
        ),
    )
    op.create_index(
        "ix_deployments_service_ts", "deployments", ["org_id", "service_id", "ts"], unique=False
    )
    op.create_table(
        "log_records",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("record_hash", sa.LargeBinary(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("observed_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("severity_number", sa.SmallInteger(), nullable=False),
        sa.Column("severity_text", sa.String(length=32), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.LargeBinary(), nullable=True),
        sa.Column("span_id", sa.LargeBinary(), nullable=True),
        sa.Column("event_name", sa.String(length=128), nullable=True),
        sa.Column("exception_type", sa.String(length=256), nullable=True),
        sa.Column("exception_message", sa.String(length=2048), nullable=True),
        sa.Column("exception_stacktrace", sa.Text(), nullable=True),
        sa.Column("service_instance_id", sa.String(length=128), nullable=True),
        sa.Column("service_version", sa.String(length=64), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(record_hash) = 32", name=op.f("ck_log_records_record_hash_length")
        ),
        sa.CheckConstraint(
            "span_id IS NULL OR octet_length(span_id) = 8",
            name=op.f("ck_log_records_span_id_length"),
        ),
        sa.CheckConstraint(
            "trace_id IS NULL OR octet_length(trace_id) = 16",
            name=op.f("ck_log_records_trace_id_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_log_records_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "record_hash", "ts", name="pk_log_records"),
        postgresql_partition_by="RANGE (ts)",
    )
    op.create_index(
        "ix_log_records_service_window", "log_records", ["org_id", "service_id", "ts"], unique=False
    )
    op.create_index(
        "ix_log_records_trace",
        "log_records",
        ["org_id", "trace_id"],
        unique=False,
        postgresql_where=sa.text("trace_id IS NOT NULL"),
    )
    op.create_table(
        "metric_series",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("series_hash", sa.LargeBinary(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("is_monotonic", sa.Boolean(), nullable=False),
        sa.Column(
            "attributes",
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
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind BETWEEN 1 AND 3", name=op.f("ck_metric_series_kind_valid")),
        sa.CheckConstraint(
            "octet_length(series_hash) = 32", name=op.f("ck_metric_series_series_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_metric_series_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_series")),
        sa.UniqueConstraint("org_id", "id", name="uq_metric_series_org_id_id"),
        sa.UniqueConstraint("org_id", "series_hash", name="uq_metric_series_org_id_series_hash"),
    )
    op.create_index(
        "ix_metric_series_service_name",
        "metric_series",
        ["org_id", "service_id", "name"],
        unique=False,
    )
    op.create_table(
        "spans",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("trace_id", sa.LargeBinary(), nullable=False),
        sa.Column("span_id", sa.LargeBinary(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("parent_span_id", sa.LargeBinary(), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Double(), nullable=False),
        sa.Column("status_code", sa.SmallInteger(), nullable=False),
        sa.Column("status_message", sa.String(length=1024), nullable=True),
        sa.Column("http_method", sa.String(length=16), nullable=True),
        sa.Column("http_route", sa.String(length=256), nullable=True),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("db_system", sa.String(length=64), nullable=True),
        sa.Column("server_address", sa.String(length=256), nullable=True),
        sa.Column("peer_service", sa.String(length=128), nullable=True),
        sa.Column("exception_type", sa.String(length=256), nullable=True),
        sa.Column("exception_message", sa.String(length=2048), nullable=True),
        sa.Column("service_instance_id", sa.String(length=128), nullable=True),
        sa.Column("service_version", sa.String(length=64), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("kind BETWEEN 0 AND 5", name=op.f("ck_spans_kind_valid")),
        sa.CheckConstraint("octet_length(span_id) = 8", name=op.f("ck_spans_span_id_length")),
        sa.CheckConstraint("octet_length(trace_id) = 16", name=op.f("ck_spans_trace_id_length")),
        sa.CheckConstraint(
            "parent_span_id IS NULL OR octet_length(parent_span_id) = 8",
            name=op.f("ck_spans_parent_span_id_length"),
        ),
        sa.CheckConstraint("status_code BETWEEN 0 AND 2", name=op.f("ck_spans_status_code_valid")),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_spans_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "trace_id", "span_id", "start_time", name="pk_spans"),
        postgresql_partition_by="RANGE (start_time)",
    )
    op.create_index(
        "ix_spans_error_window",
        "spans",
        ["org_id", "service_id", "start_time"],
        unique=False,
        postgresql_where=sa.text("status_code = 2"),
    )
    op.create_index(
        "ix_spans_server_window",
        "spans",
        ["org_id", "service_id", "start_time"],
        unique=False,
        postgresql_where=sa.text("kind = 2"),
    )
    op.create_table(
        "ingest_usage",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("key_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("signal", sa.String(length=8), nullable=False),
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "signal IN ('traces', 'logs', 'metrics')", name=op.f("ck_ingest_usage_signal_valid")
        ),
        sa.ForeignKeyConstraint(
            ["key_id"], ["service_api_keys.id"], name="fk_ingest_usage_key", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "service_id"],
            ["services.org_id", "services.id"],
            name="fk_ingest_usage_service",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "key_id", "signal", "minute", "outcome", name="pk_ingest_usage"
        ),
    )
    op.create_index(
        "ix_ingest_usage_service_minute",
        "ingest_usage",
        ["org_id", "service_id", "minute"],
        unique=False,
    )
    op.create_table(
        "metric_points",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value", sa.Double(), nullable=True),
        sa.Column("count", sa.BigInteger(), nullable=True),
        sa.Column("sum", sa.Double(), nullable=True),
        sa.Column("bucket_counts", postgresql.ARRAY(sa.BigInteger()), nullable=True),
        sa.Column("explicit_bounds", postgresql.ARRAY(sa.Double()), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id", "series_id"],
            ["metric_series.org_id", "metric_series.id"],
            name="fk_metric_points_series",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "series_id", "ts", name="pk_metric_points"),
        postgresql_partition_by="RANGE (ts)",
    )


def downgrade() -> None:
    op.drop_table("metric_points")
    op.drop_index("ix_ingest_usage_service_minute", table_name="ingest_usage")
    op.drop_table("ingest_usage")
    op.drop_index(
        "ix_spans_server_window", table_name="spans", postgresql_where=sa.text("kind = 2")
    )
    op.drop_index(
        "ix_spans_error_window", table_name="spans", postgresql_where=sa.text("status_code = 2")
    )
    op.drop_table("spans")
    op.drop_index("ix_metric_series_service_name", table_name="metric_series")
    op.drop_table("metric_series")
    op.drop_index(
        "ix_log_records_trace",
        table_name="log_records",
        postgresql_where=sa.text("trace_id IS NOT NULL"),
    )
    op.drop_index("ix_log_records_service_window", table_name="log_records")
    op.drop_table("log_records")
    op.drop_index("ix_deployments_service_ts", table_name="deployments")
    op.drop_table("deployments")
