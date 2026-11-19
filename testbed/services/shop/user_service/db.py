from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, func

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("name", String(100), nullable=False),
    Column("password_hash", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
