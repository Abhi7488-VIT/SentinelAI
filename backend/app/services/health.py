"""Readiness: are the dependencies this process needs to serve traffic reachable?"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from app.core.logging import get_logger

logger = get_logger("app.health")

CHECK_TIMEOUT_SECONDS = 2.0

Probe = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: Literal["ok", "error"]
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    checks: dict[str, CheckResult]


async def _run(name: str, probe: Probe, per_check_timeout: float) -> CheckResult:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(probe(), timeout=per_check_timeout)
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000, 2)
        # Only the exception class: messages can contain hosts/DSNs.
        error = "Timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        logger.warning("health.check_failed", check=name, error=error, latency_ms=latency)
        return CheckResult(status="error", latency_ms=latency, error=error)
    return CheckResult(status="ok", latency_ms=round((time.perf_counter() - start) * 1000, 2))


async def check_readiness(
    probes: dict[str, Probe], *, per_check_timeout: float = CHECK_TIMEOUT_SECONDS
) -> Readiness:
    names = list(probes)
    results = await asyncio.gather(*(_run(n, probes[n], per_check_timeout) for n in names))
    checks = dict(zip(names, results, strict=True))
    return Readiness(ready=all(r.status == "ok" for r in results), checks=checks)
