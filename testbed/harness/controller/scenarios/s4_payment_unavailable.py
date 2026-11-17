"""Scenario 4: the order -> payment link refuses connections (dependency_unavailable)."""

from __future__ import annotations

from harness.controller.plugin import Scenario
from harness.controller.primitives import Context, network_link

PROXY = "order-to-payment"


class PaymentUnavailable(Scenario):
    id = "payment-unavailable"

    async def reset(self, ctx: Context) -> None:
        await network_link(ctx, PROXY, enabled=True)

    async def trigger(self, ctx: Context) -> None:
        await network_link(ctx, PROXY, enabled=False)

    async def stop(self, ctx: Context) -> None:
        await network_link(ctx, PROXY, enabled=True)


PLUGIN = PaymentUnavailable()
