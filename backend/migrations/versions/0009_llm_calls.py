"""Phase 4 T1: llm_calls (one row per LLM call attempt; usage only, no text).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=True),
        sa.Column("node", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND latency_ms >= 0",
            name=op.f("ck_llm_calls_non_negative"),
        ),
        sa.CheckConstraint(
            "outcome IN ('ok', 'invalid_output', 'timeout', 'transport_error', 'fatal_error', "
            "'refusal')",
            name=op.f("ck_llm_calls_outcome_valid"),
        ),
        sa.CheckConstraint(
            "provider IN ('fake', 'anthropic')", name=op.f("ck_llm_calls_provider_valid")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_llm_calls_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(
        "ix_llm_calls_org_investigation",
        "llm_calls",
        ["org_id", "investigation_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_org_investigation", table_name="llm_calls")
    op.drop_table("llm_calls")
