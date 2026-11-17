"""Celery application for background work.

Tasks are declared with `@shared_task` in `app.worker.tasks`, so tests can bind them to their
own Celery app configured for the test Redis without importing this module (which reads the
real settings at import time).

Worker:      celery -A app.worker.celery_app worker --loglevel=INFO
Healthcheck: celery -A app.worker.celery_app inspect ping -d celery@$HOSTNAME
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.worker.config import configure_celery


def create_celery(settings: Settings | None = None) -> Celery:
    app = Celery("sentinelai", include=["app.worker.tasks"])
    return configure_celery(app, settings or get_settings())


@setup_logging.connect
def _setup_logging(**_: object) -> None:
    # Use our JSON logging instead of Celery's default formatter.
    configure_logging(get_settings().log_level)


celery_app = create_celery()
