"""order-service: product catalogue, orders, order history and sales reports."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from shop.common.app import create_service_app
from shop.common.db import Database
from shop.common.downstream import Downstream
from shop.common.errors import NotFoundError, PaymentDeclinedError, ShopError
from shop.common.settings import ServiceSettings
from shop.order_service.db import orders, products

logger = logging.getLogger("shop.orders")

REPORT_CHUNK = 500


class Product(BaseModel):
    sku: str
    name: str
    category: str
    description: str
    price_cents: int


class OrderItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=1, le=10)


class OrderIn(BaseModel):
    user_id: int = Field(gt=0)
    items: list[OrderItemIn] = Field(min_length=1, max_length=10)
    shipping_address: str = Field(min_length=1, max_length=300)


class OrderOut(BaseModel):
    id: int
    user_id: int
    status: str
    total_cents: int
    items: list[dict[str, Any]]
    created_at: datetime


class SalesReport(BaseModel):
    days: int
    orders: int
    paid_orders: int
    revenue_cents: int


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_payments(request: Request) -> Downstream:
    payments: Downstream = request.app.state.payments
    return payments


DbDep = Annotated[Database, Depends(get_db)]
PaymentsDep = Annotated[Downstream, Depends(get_payments)]


def _order_out(row: Any) -> OrderOut:
    return OrderOut(
        id=row.id,
        user_id=row.user_id,
        status=row.status,
        total_cents=row.total_cents,
        items=row.items,
        created_at=row.created_at,
    )


def create_app() -> FastAPI:
    settings = ServiceSettings.for_service("order-service")
    if settings.payment_service_url is None:
        raise RuntimeError("PAYMENT_SERVICE_URL is required")
    payment_url = settings.payment_service_url

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings)
        db.instrument(app.state.telemetry)
        await db.check()
        app.state.db = db
        app.state.payments = Downstream("payment-service", payment_url, settings.http_timeout_s)
        yield
        await app.state.payments.close()
        await db.close()

    app = create_service_app(settings, lifespan)
    orders_counter = app.state.telemetry.meter().create_counter(
        "shop.orders", unit="{order}", description="Orders by final status."
    )

    @app.get("/products", response_model=list[Product])
    async def list_products(
        db: DbDep,
        category: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 24,
    ) -> list[Product]:
        stmt = select(products).order_by(products.c.id).limit(limit)
        if category:
            stmt = stmt.where(products.c.category == category)
        async with db.connection() as conn:
            rows = (await conn.execute(stmt)).all()
        return [Product.model_validate(r, from_attributes=True) for r in rows]

    @app.get("/products/{sku}", response_model=Product)
    async def get_product(sku: str, db: DbDep) -> Product:
        async with db.connection() as conn:
            row = (await conn.execute(select(products).where(products.c.sku == sku))).first()
        if row is None:
            raise NotFoundError(f"No product {sku}.")
        return Product.model_validate(row, from_attributes=True)

    @app.get("/orders", response_model=list[OrderOut])
    async def order_history(
        db: DbDep,
        user_id: Annotated[int, Query(gt=0)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> list[OrderOut]:
        stmt = (
            select(orders)
            .where(orders.c.user_id == user_id)
            .order_by(orders.c.created_at.desc())
            .limit(limit)
        )
        async with db.connection() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_order_out(r) for r in rows]

    @app.post("/orders", response_model=OrderOut, status_code=201)
    async def create_order(body: OrderIn, db: DbDep, payments_api: PaymentsDep) -> OrderOut:
        skus = sorted({i.sku for i in body.items})
        async with db.connection() as conn:
            prices = {
                r.sku: r.price_cents
                for r in await conn.execute(
                    select(products.c.sku, products.c.price_cents).where(products.c.sku.in_(skus))
                )
            }
            missing = [s for s in skus if s not in prices]
            if missing:
                raise NotFoundError(f"Unknown products: {', '.join(missing)}.")
            items = [{"sku": i.sku, "qty": i.qty, "price_cents": prices[i.sku]} for i in body.items]
            total = sum(i["qty"] * i["price_cents"] for i in items)
            order_id = (
                await conn.execute(
                    orders.insert()
                    .values(
                        user_id=body.user_id,
                        status="pending",
                        total_cents=total,
                        items=items,
                        shipping_address=body.shipping_address,
                    )
                    .returning(orders.c.id)
                )
            ).scalar_one()
            await conn.commit()
        # The connection is back in the pool before the (slower) payment call.
        try:
            payment = await payments_api.post_json(
                "/payments/authorize", {"order_id": order_id, "amount_cents": total}
            )
            status = "paid" if payment["status"] == "approved" else "declined"
        except ShopError:
            status = "failed"
            await _set_status(db, order_id, status)
            logger.warning(
                "order failed: payment could not be authorized",
                extra={"order.id": order_id, "order.status": status},
            )
            orders_counter.add(1, {"order.status": status})
            raise
        await _set_status(db, order_id, status)
        orders_counter.add(1, {"order.status": status})
        logger.info(
            "order %s",
            "placed" if status == "paid" else status,
            extra={"order.id": order_id, "order.status": status, "order.total_cents": total},
        )
        if status == "declined":
            raise PaymentDeclinedError(
                f"Payment declined ({payment.get('decline_code') or 'unknown'})."
            )
        async with db.connection() as conn:
            row = (await conn.execute(select(orders).where(orders.c.id == order_id))).one()
        return _order_out(row)

    @app.get("/internal/reports/sales", response_model=SalesReport)
    async def sales_report(
        db: DbDep,
        payments_api: PaymentsDep,
        days: Annotated[int, Query(ge=1, le=31)] = 7,
    ) -> SalesReport:
        """Sales for the last `days`, reconciled against payment records.

        Streams the period's orders in chunks and asks payment-service about each chunk.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        count = paid = revenue = 0
        async with db.connection() as conn:
            result = await conn.stream(
                select(orders.c.id)
                .where(orders.c.created_at >= since)
                .order_by(orders.c.id)
                .execution_options(yield_per=REPORT_CHUNK)
            )
            async for chunk in result.partitions(REPORT_CHUNK):
                ids = [r.id for r in chunk]
                summary = await payments_api.post_json("/payments/summary", {"order_ids": ids})
                count += len(ids)
                paid += int(summary["approved"])
                revenue += int(summary["amount_cents"])
        return SalesReport(days=days, orders=count, paid_orders=paid, revenue_cents=revenue)

    return app


async def _set_status(db: Database, order_id: int, status: str) -> None:
    async with db.connection() as conn:
        await conn.execute(update(orders).where(orders.c.id == order_id).values(status=status))
        await conn.commit()
