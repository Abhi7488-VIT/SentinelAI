"""Aggregates all /api/v1 routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    detections,
    incidents,
    orgs,
    overview,
    projects,
    service_graph,
    services,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(orgs.router)
api_router.include_router(projects.router)
api_router.include_router(services.router)
api_router.include_router(service_graph.router)
api_router.include_router(detections.router)
api_router.include_router(incidents.router)
api_router.include_router(overview.router)
