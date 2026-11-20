"""Phase 3 T3: metric_series.temporality (OTLP aggregation temporality; decides rate math).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metric_series",
        sa.Column("temporality", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_metric_series_temporality_valid"), "metric_series", "temporality BETWEEN 0 AND 2"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_metric_series_temporality_valid"), "metric_series", type_="check")
    op.drop_column("metric_series", "temporality")
