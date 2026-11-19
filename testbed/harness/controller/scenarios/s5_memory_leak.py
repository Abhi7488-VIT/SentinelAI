"""Scenario 5: a cache-busting client fills api-gateway's unbounded response cache until the
container hits its 192 MB limit and is OOM-killed (memory_leak, bounded by the cgroup)."""

from __future__ import annotations

from harness.controller.plugin import Scenario
from harness.controller.primitives import Context, restart_service, traffic_pattern


class MemoryLeak(Scenario):
    id = "memory-leak"

    async def trigger(self, ctx: Context) -> None:
        await traffic_pattern(ctx, "cache-busting-client")

    async def stop(self, ctx: Context) -> None:
        await traffic_pattern(ctx, "baseline")
        # The cache only empties when the process restarts (recorded as part of recovery).
        await restart_service(ctx, "api-gateway")


PLUGIN = MemoryLeak()
