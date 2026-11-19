"""Seed telemetry rows directly (through the real repositories) for detection/correlation tests."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import SystemScope
from app.ingest.batch import SpanRow
from app.models.telemetry import SpanKind, SpanStatus
from app.repositories.telemetry import SpanRepository
from app.services.detection import run_tick

SM = async_sessionmaker[AsyncSession]


def us(t: datetime) -> int:
    return int(t.timestamp() * 1_000_000)


def server_span(
    at: datetime,
    *,
    duration_ms: float = 20.0,
    error: bool = False,
    instance: str = "i-1",
    trace: bytes | None = None,
    parent: bytes | None = None,
    exception: tuple[str, str] | None = None,
) -> SpanRow:
    exc_type, exc_msg = exception or (None, None)
    return SpanRow(
        trace_id=trace or os.urandom(16),
        span_id=os.urandom(8),
        parent_span_id=parent,
        name="GET /x",
        kind=int(SpanKind.SERVER),
        start_us=us(at),
        end_us=us(at + timedelta(milliseconds=duration_ms)),
        duration_ms=duration_ms,
        status_code=int(SpanStatus.ERROR if error else SpanStatus.UNSET),
        status_message=None,
        http_method="GET",
        http_route="/x",
        http_status_code=500 if error else 200,
        db_system=None,
        server_address=None,
        peer_service=None,
        exception_type=exc_type if error else None,
        exception_message=exc_msg if error else None,
        service_instance_id=instance,
        service_version="1.0.0",
        attributes={},
    )


async def insert_spans(sm: SM, org_id: str, service_id: str, rows: list[SpanRow]) -> None:
    async with sm() as s:
        scope = SystemScope(org_id=uuid.UUID(org_id), actor="test")
        await SpanRepository(s, scope).insert_many(uuid.UUID(service_id), rows)
        await s.commit()


async def traffic(
    sm: SM,
    org_id: str,
    service_id: str,
    start: datetime,
    end: datetime,
    *,
    rps: float = 2.0,
    error: Any = None,
    duration: Any = None,
    instance: str = "i-1",
    exception: tuple[str, str] = ("TimeoutError", "QueuePool limit of size 5 reached, 30.00s"),
) -> None:
    """Server spans every 1/rps s in [start, end). `error(t, i)` / `duration(t)` shape them."""
    rows: list[SpanRow] = []
    step = timedelta(seconds=1 / rps)
    t, i = start, 0
    while t < end:
        rows.append(
            server_span(
                t,
                duration_ms=duration(t) if duration else 20.0,
                error=bool(error(t, i)) if error else False,
                instance=instance,
                exception=exception,
            )
        )
        t += step
        i += 1
    await insert_spans(sm, org_id, service_id, rows)


async def sql(sm: SM, statement: str, **params: Any) -> Any:
    async with sm() as s:
        result = await s.execute(text(statement), params)
        await s.commit()
        return result


async def rows(sm: SM, statement: str, **params: Any) -> list[dict[str, Any]]:
    async with sm() as s:
        out = [dict(r._mapping) for r in await s.execute(text(statement), params)]
        await s.commit()
        return out


async def tick(sm: SM, eval_end: datetime, *, seen: list[str]) -> Any:
    """Run the detection tick whose evaluation time is `eval_end` (now = eval_end + 30 s);
    `seen` services get `last_seen_at = now` first (they are sending telemetry)."""
    now = eval_end + timedelta(seconds=30)
    for sid in seen:
        await sql(sm, "UPDATE services SET last_seen_at = :t WHERE id = :id", t=now, id=sid)
    async with sm() as s:
        return await run_tick(s, now=now)


async def ticks(sm: SM, start: datetime, end: datetime, *, seen: list[str]) -> list[Any]:
    out = []
    t = start
    while t <= end:
        out.append(await tick(sm, t, seen=seen))
        t += timedelta(seconds=15)
    return out
