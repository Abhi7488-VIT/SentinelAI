"""Liveness and metrics endpoints (root-level, not versioned)."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    status: str


@router.get("/health", response_model=LivenessResponse)
async def health() -> LivenessResponse:
    """Liveness: the process is up and serving. Makes no dependency calls."""
    return LivenessResponse(status="ok")


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
