"""Common FastAPI application factory for the shop services."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel

from shop.common.errors import ShopError
from shop.common.logging import configure_logging
from shop.common.settings import ServiceSettings
from shop.common.telemetry import EXCLUDED_URLS, Telemetry

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

logger = logging.getLogger("shop.app")


class Health(BaseModel):
    status: str
    service: str
    version: str


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _mark_span_error(exc: BaseException) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def create_service_app(
    settings: ServiceSettings,
    lifespan: Lifespan | None = None,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    telemetry = telemetry or Telemetry(settings)
    configure_logging(settings, extra_handler=telemetry.log_handler())

    if telemetry.enabled:
        HTTPXClientInstrumentor().instrument(
            tracer_provider=telemetry.tracer_provider, meter_provider=telemetry.meter_provider
        )
        RedisInstrumentor().instrument(tracer_provider=telemetry.tracer_provider)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("service starting", extra={"service.version": settings.service_version})
        try:
            if lifespan is None:
                yield
            else:
                async with lifespan(app):
                    yield
        finally:
            logger.info("service stopped")
            telemetry.shutdown()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.telemetry = telemetry

    @app.exception_handler(ShopError)
    async def _shop_error(_: Request, exc: ShopError) -> JSONResponse:
        if exc.status_code >= 500:
            _mark_span_error(exc)
            logger.error("request failed: %s", exc.message, extra={"error.code": exc.code})
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "validation_error", str(exc.errors()[:3]))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        _mark_span_error(exc)
        logger.exception("unhandled error: %s", type(exc).__name__)
        return error_response(500, "internal_error", "Internal server error.")

    @app.get("/healthz", response_model=Health, include_in_schema=False)
    async def healthz() -> Health:
        """Liveness: the process is up. Makes no dependency calls."""
        return Health(status="ok", service=settings.service_name, version=settings.service_version)

    if telemetry.enabled:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=EXCLUDED_URLS,
            exclude_spans=["receive", "send"],
            tracer_provider=telemetry.tracer_provider,
            meter_provider=telemetry.meter_provider,
        )
    return app
