"""Ingest process: the OTLP/HTTP receiver only, separate from the UI/API process.

Run with: uvicorn app.ingest_main:create_ingest_app --factory --port 8001

Same codebase and settings as the API (modular monolith), but its own process, event loop and DB
pool, so a telemetry flood cannot starve the API. It reads key rows only; records go through the
bounded `ingest` queue to the writer (Phase 3 T4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app import __version__
from app.api.health import router as health_router
from app.api.otlp import router as otlp_router
from app.core.config import Settings, get_settings
from app.core.errors import install_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import RateLimiter
from app.core.request_context import RequestContextMiddleware
from app.db.session import create_engine, create_sessionmaker
from app.ingest.auth import KeyResolver
from app.ingest.queue import CeleryIngestQueue, IngestQueue
from app.services.ingest import IngestService
from app.services.telemetry_maintenance import run_maintenance
from app.worker.config import configure_celery

logger = get_logger("app.ingest_main")


def _redis(url: str) -> Redis:
    return Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2, health_check_interval=30)


def create_ingest_app(
    settings: Settings | None = None, *, queue: IngestQueue | None = None
) -> FastAPI:
    """`queue` overrides the Celery queue (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings, application_name="sentinelai-ingest")
        sessionmaker = create_sessionmaker(engine)
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        redis = _redis(settings.redis_url.get_secret_value())
        broker = _redis(settings.celery_broker_url.get_secret_value())
        app.state.redis = redis
        ingest_queue = queue
        if ingest_queue is None:
            from celery import Celery

            celery = configure_celery(Celery("sentinelai-ingest"), settings)
            ingest_queue = CeleryIngestQueue(
                celery,
                broker,
                max_pending=settings.ingest_queue_max_pending,
                max_message_bytes=settings.ingest_queue_max_message_bytes,
            )
        app.state.ingest_service = IngestService(
            settings,
            KeyResolver(sessionmaker, ttl=settings.ingest_key_cache_seconds),
            RateLimiter(redis, prefix="rl"),
            ingest_queue,
        )
        # Make sure today's partitions exist before accepting telemetry (ADR 0005).
        try:
            async with sessionmaker() as session:
                await run_maintenance(session, settings)
        except Exception as exc:
            logger.warning("ingest.partition_maintenance_failed", error=type(exc).__name__)
        logger.info("ingest.startup", env=settings.env, version=__version__)
        try:
            yield
        finally:
            await broker.aclose()
            await redis.aclose()
            await engine.dispose()
            logger.info("ingest.shutdown")

    app = FastAPI(
        title="SentinelAI OTLP ingest",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(otlp_router)
    return app
