"""Create the shop's schemas and load deterministic seed data (one-shot, idempotent).

    python -m shop.seed

Volumes: 5,000 users, 300 products, ORDERS orders (default 500,000) with one payment each.
Rows are generated from SEED, streamed in batches with COPY so memory stays small.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import create_async_engine

from shop.order_service.db import metadata as orders_metadata
from shop.payment_service.db import metadata as payments_metadata
from shop.payment_service.issuer import decide
from shop.user_service.db import metadata as users_metadata
from shop.user_service.security import hash_password

logger = logging.getLogger("shop.seed")

USERS = 5_000
CATEGORIES = ("audio", "books", "garden", "kitchen", "outdoor", "toys")
ADJECTIVES = ("Compact", "Classic", "Deluxe", "Everyday", "Portable", "Studio", "Trail", "Urban")
NOUNS = {
    "audio": ("Headphones", "Speaker", "Turntable", "Microphone", "Earbuds"),
    "books": ("Cookbook", "Field Guide", "Novel", "Atlas", "Journal"),
    "garden": ("Trowel", "Planter", "Hose", "Pruner", "Seed Kit"),
    "kitchen": ("Kettle", "Skillet", "Knife Set", "Grinder", "Teapot"),
    "outdoor": ("Tent", "Backpack", "Lantern", "Hammock", "Stove"),
    "toys": ("Puzzle", "Robot Kit", "Kite", "Board Game", "Train Set"),
}
STREETS = ("Mill Lane", "Harbour Road", "Station Street", "Orchard Way", "Kings Avenue")
CITIES = ("Leeds", "Bristol", "Glasgow", "Cardiff", "Norwich", "York")
BATCH = 10_000


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def create_schema(url: str, metadata: MetaData) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    await engine.dispose()


def products(rng: random.Random) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    pid = 0
    for category in CATEGORIES:
        for n in range(50):
            pid += 1
            noun = NOUNS[category][n % len(NOUNS[category])]
            name = f"{ADJECTIVES[(n // 5) % len(ADJECTIVES)]} {noun} {n + 1}"
            desc = f"{name} for {category} lovers. " + " ".join(
                rng.choice(("Durable.", "Lightweight.", "Easy to clean.", "Two-year warranty."))
                for _ in range(3)
            )
            price = rng.randrange(499, 24_999, 50)
            rows.append(
                (pid, f"SKU-{category[:3].upper()}-{n + 1:03d}", name, category, desc, price)
            )
    return rows


def order_batches(
    rng: random.Random, seed: int, count: int, catalogue: list[tuple[Any, ...]], now: datetime
) -> Iterator[tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]]:
    """Orders spread evenly over the past year (ids ascending in time) and their payments."""
    start = now - timedelta(days=365)
    step = timedelta(days=365) / count
    orders: list[tuple[Any, ...]] = []
    pays: list[tuple[Any, ...]] = []
    for oid in range(1, count + 1):
        created = start + step * oid
        items = []
        for _ in range(rng.randint(1, 4)):
            product = catalogue[rng.randrange(len(catalogue))]
            items.append({"sku": product[1], "qty": rng.randint(1, 3), "price_cents": product[5]})
        total = sum(i["qty"] * i["price_cents"] for i in items)
        decision = decide(seed, oid)
        status = "paid" if decision.approved else "declined"
        address = (
            f"{rng.randint(1, 240)} {rng.choice(STREETS)}, {rng.choice(CITIES)} "
            f"{rng.choice('ABCDEFGHJKLMNPR')}{rng.randint(1, 29)} {rng.randint(1, 9)}"
            f"{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}"
        )
        orders.append(
            (oid, rng.randint(1, USERS), status, total, json.dumps(items), address, created)
        )
        pays.append(
            (
                oid,
                oid,
                total,
                "approved" if decision.approved else "declined",
                decision.decline_code,
                created,
            )
        )
        if len(orders) == BATCH:
            yield orders, pays
            orders, pays = [], []
    if orders:
        yield orders, pays


async def seed(seed_value: int, order_count: int) -> None:
    users_url = os.environ["USERS_DATABASE_URL"]
    orders_url = os.environ["ORDERS_DATABASE_URL"]
    payments_url = os.environ["PAYMENTS_DATABASE_URL"]
    for url, md in (
        (users_url, users_metadata),
        (orders_url, orders_metadata),
        (payments_url, payments_metadata),
    ):
        await create_schema(url, md)

    users_db = await asyncpg.connect(asyncpg_dsn(users_url))
    orders_db = await asyncpg.connect(asyncpg_dsn(orders_url))
    payments_db = await asyncpg.connect(asyncpg_dsn(payments_url))
    try:
        if await orders_db.fetchval("SELECT count(*) FROM orders") > 0:
            logger.info("seed data already present; nothing to do")
            return
        rng = random.Random(seed_value)  # noqa: S311  (reproducible test data, not crypto)
        t0 = time.perf_counter()

        user_rows = [
            (
                n,
                f"user{n}@shop.test",
                f"Customer {n}",
                hash_password(f"pw-{n}", salt=rng.randbytes(16)),
            )
            for n in range(1, USERS + 1)
        ]
        await users_db.copy_records_to_table(
            "users", records=user_rows, columns=["id", "email", "name", "password_hash"]
        )
        await users_db.execute("SELECT setval('users_id_seq', (SELECT max(id) FROM users))")

        catalogue = products(rng)
        await orders_db.copy_records_to_table(
            "products",
            records=catalogue,
            columns=["id", "sku", "name", "category", "description", "price_cents"],
        )

        now = datetime.now(UTC)
        done = 0
        for order_rows, pay_rows in order_batches(rng, seed_value, order_count, catalogue, now):
            await orders_db.copy_records_to_table(
                "orders",
                records=order_rows,
                columns=[
                    "id",
                    "user_id",
                    "status",
                    "total_cents",
                    "items",
                    "shipping_address",
                    "created_at",
                ],
            )
            await payments_db.copy_records_to_table(
                "payments",
                records=pay_rows,
                columns=["id", "order_id", "amount_cents", "status", "decline_code", "created_at"],
            )
            done += len(order_rows)
            if done % 100_000 == 0:
                logger.info("seeded %d orders", done)
        await orders_db.execute("SELECT setval('orders_id_seq', (SELECT max(id) FROM orders))")
        await payments_db.execute(
            "SELECT setval('payments_id_seq', (SELECT max(id) FROM payments))"
        )
        for conn in (users_db, orders_db, payments_db):
            await conn.execute("ANALYZE")
        logger.info(
            "seed complete: %d users, %d products, %d orders in %.1fs",
            USERS,
            len(catalogue),
            order_count,
            time.perf_counter() - t0,
        )
    finally:
        for conn in (users_db, orders_db, payments_db):
            await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    asyncio.run(seed(int(os.environ.get("SEED", "42")), int(os.environ.get("ORDERS", "500000"))))


if __name__ == "__main__":
    main()
