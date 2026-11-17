"""Scenario 3: user-service 2.4.0 ships config pointing at a database that does not exist
(bad_deployment). Its pods crash on startup; the rollback is a second deploy of 2.3.0."""

from __future__ import annotations

from harness.controller.plugin import Scenario
from harness.controller.primitives import Context, deploy, release_of

SERVICE = "user-service"
GOOD = "2.3.0"
BAD = "2.4.0"
BAD_CONFIG = {"DATABASE_NAME": "users_v2"}
# Docker's restart back-off for a crash-looping container grows towards a minute, so the
# rollback waits longer than a normal deploy for the good release to come up.
ROLLBACK_WAIT_S = 120.0


class FailedDeploymentBadConfig(Scenario):
    id = "failed-deployment-bad-config"

    async def reset(self, ctx: Context) -> None:
        current = release_of(ctx, SERVICE)
        if current is not None and current != (GOOD, {}):
            await deploy(ctx, SERVICE, GOOD, {}, wait_s=ROLLBACK_WAIT_S)

    async def trigger(self, ctx: Context) -> None:
        await deploy(ctx, SERVICE, BAD, BAD_CONFIG)  # expected to end "failed"

    async def stop(self, ctx: Context) -> None:
        await deploy(ctx, SERVICE, GOOD, {}, wait_s=ROLLBACK_WAIT_S)


PLUGIN = FailedDeploymentBadConfig()
