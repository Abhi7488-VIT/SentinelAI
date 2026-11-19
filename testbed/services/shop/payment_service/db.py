from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, MetaData, String, Table, func

metadata = MetaData()

payments = Table(
    "payments",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("order_id", BigInteger, nullable=False, unique=True),
    Column("amount_cents", Integer, nullable=False),
    Column("status", String(16), nullable=False),  # approved | declined
    Column("decline_code", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

Index("ix_payments_created_at", payments.c.created_at)
