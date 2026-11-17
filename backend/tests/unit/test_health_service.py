"""Readiness aggregation logic with stub probes."""

from __future__ import annotations

import asyncio

from app.services.health import check_readiness


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise ConnectionRefusedError("dsn=postgres://user:secret@host")


async def _hang() -> None:
    await asyncio.sleep(10)


async def test_all_ok_is_ready() -> None:
    result = await check_readiness({"a": _ok, "b": _ok})
    assert result.ready
    assert {c.status for c in result.checks.values()} == {"ok"}


async def test_failure_reports_class_only() -> None:
    result = await check_readiness({"db": _boom, "cache": _ok})
    assert not result.ready
    assert result.checks["db"].status == "error"
    assert result.checks["db"].error == "ConnectionRefusedError"
    assert result.checks["cache"].status == "ok"


async def test_timeout_is_bounded_and_checks_run_concurrently() -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await check_readiness({"slow1": _hang, "slow2": _hang}, per_check_timeout=0.2)
    elapsed = loop.time() - start
    assert not result.ready
    assert result.checks["slow1"].error == "Timeout"
    assert elapsed < 0.4  # concurrent, not 2 x 0.2s
