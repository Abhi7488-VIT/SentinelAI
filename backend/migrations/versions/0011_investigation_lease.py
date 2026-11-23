"""Phase 4 T3: investigation lease + lifecycle columns; llm_calls -> investigations FK.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23 19:53:32.425667+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigations",
        sa.Column(
            "incomplete_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "investigations", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "investigations", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("investigations", sa.Column("lease_owner", sa.String(length=128), nullable=True))
    op.add_column(
        "investigations", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_investigations_lease_pair"),
        "investigations",
        "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
    )
    op.create_index(
        "ix_investigations_running_lease",
        "investigations",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_foreign_key(
        "fk_llm_calls_investigation",
        "llm_calls",
        "investigations",
        ["org_id", "investigation_id"],
        ["org_id", "id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_investigations_lease_pair"), "investigations", type_="check")
    op.drop_constraint("fk_llm_calls_investigation", "llm_calls", type_="foreignkey")
    op.drop_index(
        "ix_investigations_running_lease",
        table_name="investigations",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_column("investigations", "lease_expires_at")
    op.drop_column("investigations", "lease_owner")
    op.drop_column("investigations", "finished_at")
    op.drop_column("investigations", "started_at")
    op.drop_column("investigations", "incomplete_reasons")
