"""payment-service: authorizes payments with the card issuer and records them."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from shop.common.app import create_service_app
from shop.common.db import Database
from shop.common.settings import ServiceSettings
from shop.payment_service import issuer
from shop.payment_service.db import payments

logger = logging.getLogger("shop.payments")


class AuthorizeRequest(BaseModel):
    order_id: int = Field(gt=0)
    amount_cents: int = Field(gt=0)


class PaymentOut(BaseModel):
    order_id: int
    amount_cents: int
    status: Literal["approved", "declined"]
    decline_code: str | None


class SummaryRequest(BaseModel):
    order_ids: list[int] = Field(max_length=1000)


class SummaryOut(BaseModel):
    approved: int
    declined: int
    amount_cents: int


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


DbDep = Annotated[Database, Depends(get_db)]


def create_app() -> FastAPI:
    settings = ServiceSettings.for_service("payment-service")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings)
        db.instrument(app.state.telemetry)
        await db.check()
        app.state.db = db
        yield
        await db.close()

    app = create_service_app(settings, lifespan)
    payments_counter = app.state.telemetry.meter().create_counter(
        "shop.payments", unit="{payment}", description="Authorizations by outcome."
    )

    @app.post("/payments/authorize", response_model=PaymentOut)
    async def authorize(body: AuthorizeRequest, db: DbDep) -> PaymentOut:
        """Idempotent per order: a retry returns the original decision."""
        decision = await issuer.authorize(settings.seed, body.order_id)
        status = "approved" if decision.approved else "declined"
        payments_counter.add(1, {"payment.status": status})
        logger.info(
            "payment %s",
            status,
            extra={
                "order.id": body.order_id,
                "payment.status": status,
                "payment.decline_code": decision.decline_code or "",
            },
        )
        async with db.connection() as conn:
            stmt = (
                insert(payments)
                .values(
                    order_id=body.order_id,
                    amount_cents=body.amount_cents,
                    status=status,
                    decline_code=decision.decline_code,
                )
                .on_conflict_do_nothing(index_elements=["order_id"])
            )
            await conn.execute(stmt)
            await conn.commit()
            row = (
                await conn.execute(
                    select(
                        payments.c.amount_cents, payments.c.status, payments.c.decline_code
                    ).where(payments.c.order_id == body.order_id)
                )
            ).one()
        return PaymentOut(
            order_id=body.order_id,
            amount_cents=row.amount_cents,
            status=row.status,
            decline_code=row.decline_code,
        )

    @app.post("/payments/summary", response_model=SummaryOut)
    async def summary(body: SummaryRequest, db: DbDep) -> SummaryOut:
        if not body.order_ids:
            return SummaryOut(approved=0, declined=0, amount_cents=0)
        async with db.connection() as conn:
            rows = (
                await conn.execute(
                    select(payments.c.status, payments.c.amount_cents).where(
                        payments.c.order_id.in_(body.order_ids)
                    )
                )
            ).all()
        approved = [r for r in rows if r.status == "approved"]
        return SummaryOut(
            approved=len(approved),
            declined=len(rows) - len(approved),
            amount_cents=sum(r.amount_cents for r in approved),
        )

    return app
