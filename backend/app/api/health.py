"""Liveness, readiness and metrics endpoints (root-level, not versioned)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.db.probes import ping_postgres, ping_redis
from app.services.health import check_readiness

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class CheckOut(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, CheckOut]


@router.get("/health", response_model=LivenessResponse)
async def health() -> LivenessResponse:
    """Liveness: the process is up and serving. Makes no dependency calls."""
    return LivenessResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
async def ready(request: Request) -> JSONResponse:
    """Readiness: Postgres and Redis reachable. 503 with per-dependency status otherwise."""
    state = request.app.state
    result = await check_readiness(
        {
            "postgres": lambda: ping_postgres(state.engine),
            "redis": lambda: ping_redis(state.redis),
        }
    )
    body = ReadinessResponse(
        status="ready" if result.ready else "degraded",
        checks={
            name: CheckOut(status=c.status, latency_ms=c.latency_ms, error=c.error)
            for name, c in result.checks.items()
        },
    )
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=200 if result.ready else 503
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
