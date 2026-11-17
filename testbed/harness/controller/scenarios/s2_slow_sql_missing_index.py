"""Scenario 2: the order-history index is dropped, so GET /orders scans 500k rows (slow_query)."""

from __future__ import annotations

from harness.controller.plugin import Scenario
from harness.controller.primitives import Context, sql_change

CHANGE = "drop-order-history-indexes"


class SlowSqlMissingIndex(Scenario):
    id = "slow-sql-missing-index"

    async def reset(self, ctx: Context) -> None:
        await sql_change(ctx, CHANGE, revert=True)  # CREATE INDEX IF NOT EXISTS: quiet if present

    async def trigger(self, ctx: Context) -> None:
        await sql_change(ctx, CHANGE)

    async def stop(self, ctx: Context) -> None:
        await sql_change(ctx, CHANGE, revert=True)


PLUGIN = SlowSqlMissingIndex()
