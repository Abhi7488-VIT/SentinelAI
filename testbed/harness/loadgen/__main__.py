"""Load generator process: runs the traffic and a small control API on :8089.

GET  /healthz       liveness
GET  /stats         current mix, running totals, in-flight, dropped
GET  /mix           current mix name
PUT  /mix {"name", "seed"?}  switch mix (schedules restart from t=0; seed optional)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from harness.loadgen.mixes import MIXES
from harness.loadgen.runner import LoadGenerator


class MixIn(BaseModel):
    name: str
    seed: int | None = None  # None keeps the current seed


def build_api(gen: LoadGenerator) -> FastAPI:
    api = FastAPI(title="loadgen", docs_url=None, redoc_url=None)

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/stats")
    async def stats() -> dict[str, Any]:
        return gen.snapshot()

    @api.get("/mix")
    async def mix() -> dict[str, Any]:
        return {"mix": gen.mix.name, "available": sorted(MIXES)}

    @api.put("/mix")
    async def set_mix(body: MixIn) -> dict[str, Any]:
        try:
            gen.set_mix(body.name, seed=body.seed)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"mix": gen.mix.name, "seed": gen.seed}

    return api


async def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    gen = LoadGenerator(
        target_url=os.environ.get("TARGET_URL", "http://frontend:8000"),
        order_service_url=os.environ.get("ORDER_SERVICE_URL", "http://order-service:8000"),
        seed=int(os.environ.get("SEED", "42")),
        mix=os.environ.get("LOADGEN_MIX", "baseline"),
    )
    server = uvicorn.Server(
        uvicorn.Config(build_api(gen), host="0.0.0.0", port=8089, log_level="warning")  # noqa: S104
    )
    gen.set_mix(gen.mix.name)
    try:
        await asyncio.gather(server.serve(), gen.report_forever())
    finally:
        await gen.close()


if __name__ == "__main__":
    asyncio.run(main())
