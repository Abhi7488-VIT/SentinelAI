"""Common FastAPI application factory for the shop services."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from shop.common.settings import ServiceSettings


class Health(BaseModel):
    status: str
    service: str
    version: str


def create_service_app(settings: ServiceSettings) -> FastAPI:
    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings

    @app.get("/healthz", response_model=Health, include_in_schema=False)
    async def healthz() -> Health:
        """Liveness: the process is up. Makes no dependency calls."""
        return Health(status="ok", service=settings.service_name, version=settings.service_version)

    return app
