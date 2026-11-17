"""Celery round-trip through a real Redis broker with an in-process worker."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker

from app.worker.config import configure_celery
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def celery_test_app() -> Iterator[Celery]:
    import app.worker.tasks  # noqa: F401  (registers shared tasks on the current app)

    test_app = configure_celery(Celery("sentinelai-test"), make_settings())
    test_app.conf.update(task_default_queue="test-roundtrip")
    test_app.set_current()
    with start_worker(test_app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        yield test_app


def test_ping_task_round_trip(celery_test_app: Celery) -> None:
    result = celery_test_app.send_task("system.ping")
    payload = result.get(timeout=15)
    assert payload["status"] == "pong"
    assert payload["at"].endswith("+00:00")
