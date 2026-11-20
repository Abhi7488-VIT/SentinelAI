"""Telemetry storage (ADR 0005): daily partitions, routing, dedup keys, retention, locking."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.telemetry import PARTITIONED_TABLES
from app.repositories import telemetry_partitions as parts
from app.services.telemetry_maintenance import run_maintenance

T0 = datetime(2031, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def partitions_reset(
    sessionmaker: async_sessionmaker[AsyncSession], test_settings: Settings
) -> AsyncIterator[None]:
    """Start with no partitions; afterwards restore the normal window around the real now."""

    async def drop_all() -> None:
        async with sessionmaker() as s:
            for parent in PARTITIONED_TABLES:
                for p in await parts.list_partitions(s, parent):
                    await parts.drop_partition(s, p)
            await s.commit()

    await drop_all()
    yield
    await drop_all()
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


async def _exec(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def _tenant_with_service(engine: AsyncEngine, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    org, project, service = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _exec(
        engine, "INSERT INTO organizations (id, name, slug) VALUES (:o, 'O', :s)", o=org, s=slug
    )
    await _exec(
        engine,
        "INSERT INTO projects (id, org_id, name, slug) VALUES (:p, :o, 'P', 'p')",
        p=project,
        o=org,
    )
    await _exec(
        engine,
        "INSERT INTO services (id, org_id, project_id, name, environment) "
        "VALUES (:s, :o, :p, 'order-service', 'production')",
        s=service,
        o=org,
        p=project,
    )
    return org, service


_INSERT_SPAN = (
    "INSERT INTO spans (org_id, trace_id, span_id, start_time, service_id, name, kind, end_time, "
    "duration_ms, status_code) VALUES (:org, :trace, :span, :start, :svc, 'GET /orders', 2, "
    ":start, 1.5, 0) ON CONFLICT DO NOTHING"
)


def _names(parent: str, *days: str) -> set[str]:
    return {f"{parent}_p{d}" for d in days}


async def test_maintenance_creates_window_and_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    partitions_reset: None,
) -> None:
    async with sessionmaker() as s:
        first = await run_maintenance(s, test_settings, now=T0)
    assert first.ran
    window = ("20310228", "20310301", "20310302", "20310303")  # today-1 .. today+2
    expected = set().union(*(_names(p, *window) for p in PARTITIONED_TABLES))
    assert set(first.created) == expected
    assert first.dropped == []

    async with sessionmaker() as s:
        second = await run_maintenance(s, test_settings, now=T0)
    assert second.ran
    assert second.created == []
    assert second.dropped == []


async def test_rows_route_to_their_day_and_dedup(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    partitions_reset: None,
) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings, now=T0)
    org, svc = await _tenant_with_service(engine, "org-a")
    trace, span = uuid.uuid4().bytes, uuid.uuid4().bytes[:8]
    start = T0 + timedelta(hours=13)  # 2031-03-02 01:00 UTC
    for _ in range(2):  # a re-sent batch
        await _exec(engine, _INSERT_SPAN, org=org, trace=trace, span=span, start=start, svc=svc)
    assert await _scalar(engine, "SELECT count(*) FROM spans") == 1
    where = await _scalar(engine, "SELECT tableoid::regclass::text FROM spans")
    assert where == "spans_p20310302"
    # The partition inherited the parent's indexes.
    idx = await _scalar(
        engine,
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'spans_p20310302'",
    )
    assert idx >= 3  # primary key + two partial window indexes


async def test_insert_outside_the_window_fails_loudly(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    partitions_reset: None,
) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings, now=T0)
    org, svc = await _tenant_with_service(engine, "org-a")
    with pytest.raises(DBAPIError, match="no partition of relation"):
        await _exec(
            engine,
            _INSERT_SPAN,
            org=org,
            trace=uuid.uuid4().bytes,
            span=b"\x01" * 8,
            start=T0 + timedelta(days=30),
            svc=svc,
        )


async def test_retention_drops_only_expired_days_per_table(
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    partitions_reset: None,
) -> None:
    assert test_settings.telemetry_retention_spans_days == 3
    assert test_settings.telemetry_retention_logs_days == 3
    assert test_settings.telemetry_retention_metrics_days == 7
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings, now=T0)  # 02-28 .. 03-03
    async with sessionmaker() as s:
        later = await run_maintenance(s, test_settings, now=T0 + timedelta(days=5))  # 03-06
    # spans/logs keep days >= 03-03 (3 days back); metrics keep days >= 02-27 (7 days back).
    expired = ("20310228", "20310301", "20310302")
    assert set(later.dropped) == _names("spans", *expired) | _names("log_records", *expired)
    async with sessionmaker() as s:
        metric_days = [p.day.isoformat() for p in await parts.list_partitions(s, "metric_points")]
        span_days = [p.day.isoformat() for p in await parts.list_partitions(s, "spans")]
    assert metric_days[0] == "2031-02-28"
    # 03-03 survives from the first window; 03-05..03-08 is the new one (03-04 was in neither).
    assert span_days == ["2031-03-03", "2031-03-05", "2031-03-06", "2031-03-07", "2031-03-08"]


async def test_concurrent_run_skips(
    sessionmaker: async_sessionmaker[AsyncSession], test_settings: Settings
) -> None:
    async with sessionmaker() as holder:
        assert await parts.try_lock(holder)
        async with sessionmaker() as other:
            result = await run_maintenance(other, test_settings)
        assert not result.ran
        await holder.rollback()  # releases the lock
    async with sessionmaker() as s:
        assert (await run_maintenance(s, test_settings)).ran


async def test_usage_retention(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    org, svc = await _tenant_with_service(engine, "org-a")
    key = uuid.uuid4()
    await _exec(
        engine,
        "INSERT INTO service_api_keys (id, org_id, service_id, name, prefix, secret_hash) "
        "VALUES (:k, :o, :s, 'k', '0123456789ab', decode(repeat('00', 32), 'hex'))",
        k=key,
        o=org,
        s=svc,
    )
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    for minute in (now - timedelta(days=31), now - timedelta(days=1)):
        await _exec(
            engine,
            "INSERT INTO ingest_usage (org_id, key_id, service_id, signal, minute, outcome, count)"
            " VALUES (:o, :k, :s, 'traces', :m, 'accepted', 10)",
            o=org,
            k=key,
            s=svc,
            m=minute,
        )
    async with sessionmaker() as s:
        result = await run_maintenance(s, test_settings, now=now)
    assert result.usage_rows_deleted == 1
    assert await _scalar(engine, "SELECT count(*) FROM ingest_usage") == 1


async def test_telemetry_cannot_reference_another_tenants_service(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)
    org_a, _svc_a = await _tenant_with_service(engine, "org-a")
    _org_b, svc_b = await _tenant_with_service(engine, "org-b")
    with pytest.raises(IntegrityError, match="fk_spans_service"):
        await _exec(
            engine,
            _INSERT_SPAN,
            org=org_a,
            trace=uuid.uuid4().bytes,
            span=b"\x02" * 8,
            start=datetime.now(UTC),
            svc=svc_b,
        )


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("trace", b"\x00" * 15, "ck_spans_trace_id_length"),
        ("span", b"\x00" * 7, "ck_spans_span_id_length"),
    ],
)
async def test_span_id_lengths(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    column: str,
    value: bytes,
    constraint: str,
) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)
    org, svc = await _tenant_with_service(engine, "org-a")
    params: dict[str, Any] = {
        "org": org,
        "trace": uuid.uuid4().bytes,
        "span": b"\x03" * 8,
        "start": datetime.now(UTC),
        "svc": svc,
    }
    params[column] = value
    with pytest.raises(IntegrityError, match=constraint):
        await _exec(engine, _INSERT_SPAN, **params)


def test_partition_names_reject_unknown_tables() -> None:
    with pytest.raises(ValueError, match="not a partitioned"):
        parts.partition_name("users; DROP TABLE users", T0.date())


async def test_app_connections_run_without_jit(engine: AsyncEngine) -> None:
    # The test engine is built by app.db.session.create_engine, like the API's.
    assert await _scalar(engine, "SHOW jit") == "off"
