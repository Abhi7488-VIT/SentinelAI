"""Background tasks."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.ingest.batch import decode_batch
from app.ingest.dead_letter import dead_letter
from app.services.detection import run_tick
from app.services.ingest_writer import WriteResult, write_batch
from app.services.service_graph import discover_edges
from app.services.telemetry_maintenance import run_maintenance
from app.worker.db import run_with_session
from app.worker.runtime import run_in_session

logger = get_logger("app.worker.tasks")
INGEST_MAX_RETRIES = 5


@shared_task(name="system.ping")
def ping() -> dict[str, str]:
    return {"status": "pong", "at": datetime.now(UTC).isoformat()}


@shared_task(name="telemetry.maintain_partitions")
def maintain_telemetry_partitions() -> dict[str, Any]:
    """Create upcoming daily partitions and drop expired ones (ADR 0005). Idempotent."""
    settings = get_settings()
    result = run_with_session(
        settings,
        lambda session: run_maintenance(session, settings),
        application_name="sentinelai-maintenance",
    )
    return asdict(result)


@shared_task(name="graph.discover_edges")
def discover_graph_edges() -> dict[str, Any]:
    """Upsert observed dependency edges from the last 2 minutes of spans. Idempotent."""
    settings = get_settings()
    result = run_with_session(
        settings, lambda session: discover_edges(session), application_name="sentinelai-graph"
    )
    return asdict(result)


@shared_task(name="detection.tick")
def detection_tick() -> dict[str, Any]:
    """Rollups → rules → detections for the current 15-s bucket. Idempotent per bucket."""
    settings = get_settings()
    result = run_with_session(
        settings, lambda session: run_tick(session), application_name="sentinelai-detection"
    )
    return {
        "ran": result.ran,
        "eval_end": result.eval_end.isoformat() if result.eval_end else None,
        "orgs": result.orgs,
        "services": result.services,
        "evaluated": result.evaluated,
        "fired": [str(i) for i in result.fired],
        "resolved": [str(i) for i in result.resolved],
        "escalated": [str(i) for i in result.escalated],
        "duration_ms": result.duration_ms,
    }


def retry_countdown(retries: int, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff (1, 2, 4, 8, 16 s; capped at 60) with ±50% jitter."""
    return float(min(60, 2**retries)) * (0.5 + rng())


def process_ingest_payload(
    payload: str,
    settings: Settings,
    *,
    retries: int,
    write: Callable[[Settings, Any], WriteResult],
    dead: Callable[[Settings, str, str], None] = dead_letter,
) -> dict[str, Any]:
    """Decode and write one batch. Returns a summary; raises to request a retry."""
    try:
        batch = decode_batch(payload)
    except Exception:
        dead(settings, payload, "undecodable")
        return {"dead_letter": "undecodable"}
    try:
        result = write(settings, batch)
    except Exception as exc:
        if retries >= INGEST_MAX_RETRIES:
            dead(settings, payload, type(exc).__name__)
            return {"dead_letter": type(exc).__name__}
        raise
    return {
        "batch_id": str(batch.batch_id),
        "duplicate_batch": result.skipped_duplicate_batch,
        "inserted": result.inserted,
        "rejected": result.rejected,
    }


def _write(settings: Settings, batch: Any) -> WriteResult:
    return run_in_session(
        settings,
        lambda session: write_batch(session, batch, settings),
        application_name="sentinelai-ingest-writer",
    )


@shared_task(name="ingest.write_batch", bind=True, acks_late=True, max_retries=INGEST_MAX_RETRIES)
def write_ingest_batch(self: Any, payload: str) -> dict[str, Any]:
    """Consumes the `ingest` queue. Idempotent (batch id + ON CONFLICT), so retries are safe."""
    settings = get_settings()
    try:
        return process_ingest_payload(payload, settings, retries=self.request.retries, write=_write)
    except Exception as exc:
        countdown = retry_countdown(self.request.retries)
        logger.warning(
            "ingest_write_retry",
            attempt=self.request.retries + 1,
            countdown_s=round(countdown, 1),
            error=type(exc).__name__,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
