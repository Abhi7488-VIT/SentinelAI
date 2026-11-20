"""Celery configuration applied from Settings (no side effects at import)."""

from __future__ import annotations

from celery import Celery

from app.core.config import Settings


def configure_celery(app: Celery, settings: Settings) -> Celery:
    app.conf.update(
        broker_url=settings.celery_broker_url.get_secret_value(),
        result_backend=settings.celery_result_backend.get_secret_value(),
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=300,
        task_soft_time_limit=270,
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,
        timezone="UTC",
        enable_utc=True,
        task_default_queue="default",
        beat_schedule={
            # Partitions are created days ahead, so a missed run or two is harmless.
            "telemetry-maintain-partitions": {
                "task": "telemetry.maintain_partitions",
                "schedule": 600.0,
                "options": {"expires": 540},
            },
            # Observed dependency edges over the last 2 min of spans (Phase 3 T6). Windows
            # overlap, so a skipped or expired run loses nothing.
            # Detection pipeline (Phase 3 T7/T8). A late tick is dropped, not queued up: the
            # next one evaluates the next bucket, and timers measure real time.
            "detection-tick": {
                "task": "detection.tick",
                "schedule": 15.0,
                "options": {"expires": 14},
            },
            "graph-discover-edges": {
                "task": "graph.discover_edges",
                "schedule": 60.0,
                "options": {"expires": 55},
            },
        },
    )
    return app
