"""Common FastAPI application factory for the shop services."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shop.common.errors import ShopError
from shop.common.logging import configure_logging
from shop.common.settings import ServiceSettings

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

logger = logging.getLogger("shop.app")


class Health(BaseModel):
    status: str
    service: str
    version: str


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_service_app(settings: ServiceSettings, lifespan: Lifespan | None = None) -> FastAPI:
    configure_logging(settings)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service starting",
            extra={"service.version": settings.service_version},
        )
        if lifespan is None:
            yield
        else:
            async with lifespan(app):
                yield
        logger.info("service stopped")

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(ShopError)
    async def _shop_error(_: Request, exc: ShopError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("request failed: %s", exc.message, extra={"error.code": exc.code})
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "validation_error", str(exc.errors()[:3]))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", type(exc).__name__)
        return error_response(500, "internal_error", "Internal server error.")

    @app.get("/healthz", response_model=Health, include_in_schema=False)
    async def healthz() -> Health:
        """Liveness: the process is up. Makes no dependency calls."""
        return Health(status="ok", service=settings.service_name, version=settings.service_version)

    return app
