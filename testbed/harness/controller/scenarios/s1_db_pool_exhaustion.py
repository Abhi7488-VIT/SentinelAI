"""Scenario 1: DB connection-pool exhaustion in order-service (resource_exhaustion)."""

from __future__ import annotations

from harness.controller.plugin import Scenario
from harness.controller.primitives import Context, traffic_pattern


class DbPoolExhaustion(Scenario):
    id = "db-pool-exhaustion"

    async def trigger(self, ctx: Context) -> None:
        await traffic_pattern(ctx, "with-weekly-reports")

    async def stop(self, ctx: Context) -> None:
        await traffic_pattern(ctx, "baseline")


PLUGIN = DbPoolExhaustion()
