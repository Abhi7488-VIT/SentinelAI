"""Investigation Celery task policy (Phase 4 T5): retry until the cap, then dead-letter."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.investigation.runner import RunOutcome
from app.worker.config import configure_celery
from app.worker.tasks import INVESTIGATION_MAX_RETRIES, process_investigation


class Boom(Exception):  # noqa: N818
    pass


def test_success_reports_the_outcome() -> None:
    dead: list[Any] = []
    out = process_investigation(
        lambda: RunOutcome(status="incomplete", reasons=["report_not_generated"]),
        lambda c, n: dead.append((c, n)),
        retries=0,
    )
    assert out["status"] == "incomplete"
    assert dead == []


@pytest.mark.parametrize("retries", range(INVESTIGATION_MAX_RETRIES))
def test_errors_below_the_cap_request_a_retry(retries: int) -> None:
    dead: list[Any] = []

    def run() -> RunOutcome:
        raise Boom()

    with pytest.raises(Boom):
        process_investigation(run, lambda c, n: dead.append((c, n)), retries=retries)
    assert dead == []


def test_at_the_cap_the_run_is_dead_lettered_and_failed() -> None:
    dead: list[Any] = []

    def run() -> RunOutcome:
        raise Boom("db down")

    out = process_investigation(
        run, lambda c, n: dead.append((c, n)), retries=INVESTIGATION_MAX_RETRIES
    )
    assert out == {"status": "failed", "dead_letter": "Boom"}
    assert dead == [("Boom", INVESTIGATION_MAX_RETRIES + 1)]


def test_queue_routing_and_sweep_schedule(test_settings_unit: Settings) -> None:
    from celery import Celery

    app = configure_celery(Celery("t"), test_settings_unit)
    assert app.conf.task_routes["investigation.run"] == {"queue": "investigations"}
    assert app.conf.beat_schedule["investigation-sweep"]["task"] == "investigation.sweep"


@pytest.fixture
def test_settings_unit() -> Settings:
    from tests.conftest import make_settings

    return make_settings()
