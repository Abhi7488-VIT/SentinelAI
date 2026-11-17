from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sku", String(32), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("category", String(32), nullable=False),
    Column("description", Text, nullable=False),
    Column("price_cents", Integer, nullable=False),
)

Index("ix_products_category", products.c.category)

orders = Table(
    "orders",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),  # pending | paid | declined | failed
    Column("total_cents", Integer, nullable=False),
    Column("items", JSONB, nullable=False),
    Column("shipping_address", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Serves the order-history lookup (WHERE user_id = ? ORDER BY created_at DESC LIMIT n).
Index("ix_orders_user_id_created_at", orders.c.user_id, orders.c.created_at.desc())
Index("ix_orders_created_at", orders.c.created_at)
