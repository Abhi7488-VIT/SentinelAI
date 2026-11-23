"""One row per LLM call attempt (Phase 4 plan §3): usage, latency, cost, outcome.

Never holds prompt or response text (decision D7), nor any credential (A4). `investigation_id`
reaches its investigation through a composite FK (same org), or is NULL for calls outside one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

LLM_OUTCOMES = (
    "ok",
    "invalid_output",
    "timeout",
    "transport_error",
    "fatal_error",
    "refusal",
)
LLM_PROVIDERS = ("fake", "anthropic")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(_in("outcome", LLM_OUTCOMES), name="outcome_valid"),
        CheckConstraint(_in("provider", LLM_PROVIDERS), name="provider_valid"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND latency_ms >= 0",
            name="non_negative",
        ),
        Index("ix_llm_calls_org_investigation", "org_id", "investigation_id", "id"),
        ForeignKeyConstraint(
            ["org_id", "investigation_id"],
            ["investigations.org_id", "investigations.id"],
            ondelete="CASCADE",
            name="fk_llm_calls_investigation",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_llm_calls_org"),
        nullable=False,
    )
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    node: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
