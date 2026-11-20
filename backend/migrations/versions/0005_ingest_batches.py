"""Phase 3 T4: ingest_batches (processed batch ids; exactly-once writer effects).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_batches",
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_ingest_batches_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("batch_id", name=op.f("pk_ingest_batches")),
    )
    op.create_index(
        "ix_ingest_batches_processed_at", "ingest_batches", ["processed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_ingest_batches_processed_at", table_name="ingest_batches")
    op.drop_table("ingest_batches")
