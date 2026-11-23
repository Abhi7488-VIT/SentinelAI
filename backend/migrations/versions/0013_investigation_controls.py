"""Phase 4 T5: budgets, usage counters, cancellation, attempts; investigation dead letters.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-23 20:18:35.078619+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigation_dead_letters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("error_class", sa.String(length=128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_node", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            name="fk_investigation_dead_letters_investigation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_dead_letters")),
        sa.UniqueConstraint("investigation_id", name="uq_investigation_dead_letters_investigation"),
    )
    op.create_index(
        "ix_investigation_dead_letters_org_id_created_at",
        "investigation_dead_letters",
        ["org_id", "created_at"],
        unique=False,
    )
    op.add_column(
        "investigations",
        sa.Column("budget_steps", sa.Integer(), server_default="30", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column("budget_tokens", sa.Integer(), server_default="200000", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column("budget_wall_clock_s", sa.Integer(), server_default="600", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column("budget_tool_calls", sa.Integer(), server_default="60", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column("budget_llm_calls", sa.Integer(), server_default="15", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column(
            "budget_cost_usd",
            sa.Numeric(precision=10, scale=4),
            server_default="1.00",
            nullable=False,
        ),
    )
    op.add_column(
        "investigations", sa.Column("used_steps", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "investigations",
        sa.Column("used_tool_calls", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("investigations", sa.Column("last_node", sa.String(length=32), nullable=True))
    op.add_column(
        "investigations", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "investigations",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_investigations_budgets_positive"),
        "investigations",
        "budget_steps > 0 AND budget_tokens > 0 AND budget_wall_clock_s > 0"
        " AND budget_tool_calls > 0 AND budget_llm_calls > 0 AND budget_cost_usd > 0",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_investigations_budgets_positive"), "investigations", type_="check")
    op.drop_column("investigations", "cancel_requested_at")
    op.drop_column("investigations", "attempts")
    op.drop_column("investigations", "last_node")
    op.drop_column("investigations", "used_tool_calls")
    op.drop_column("investigations", "used_steps")
    op.drop_column("investigations", "budget_cost_usd")
    op.drop_column("investigations", "budget_llm_calls")
    op.drop_column("investigations", "budget_tool_calls")
    op.drop_column("investigations", "budget_wall_clock_s")
    op.drop_column("investigations", "budget_tokens")
    op.drop_column("investigations", "budget_steps")
    op.drop_index(
        "ix_investigation_dead_letters_org_id_created_at", table_name="investigation_dead_letters"
    )
    op.drop_table("investigation_dead_letters")
