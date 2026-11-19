"""Common FastAPI application factory for the shop services."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
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
        started = False
        try:
            if lifespan is None:
                started = True
                yield
            else:
                async with lifespan(app):
                    started = True
                    yield
        except Exception as exc:
            # Logged here, while the telemetry pipeline is still up (uvicorn would only log it
            # after shutdown, when nothing is exported any more).
            if not started:
                logger.exception("service failed to start: %s", exc)
            raise
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

    @app.post("/ops/restart", include_in_schema=False, status_code=202)
    async def restart(x_ops_token: str | None = Header(default=None)) -> Response:
        """Used by the deploy pipeline: exit after replying; the container restarts on the
        release now in the manifest. Excluded from tracing (operational, not product traffic)."""
        expected = settings.ops_token.get_secret_value() if settings.ops_token else None
        if not expected or not x_ops_token or not hmac.compare_digest(x_ops_token, expected):
            return error_response(403, "forbidden", "Not allowed.")
        logger.info("restart requested by the deploy pipeline")
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        return Response(status_code=202)

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
