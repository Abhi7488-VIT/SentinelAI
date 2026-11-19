"""Writer task policy: retry with backoff + jitter, dead-letter after the last attempt."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ingest.batch import NormalizedBatch, encode_batch
from app.services.ingest_writer import WriteResult
from app.worker.tasks import INGEST_MAX_RETRIES, process_ingest_payload, retry_countdown
from tests.conftest import make_settings

SETTINGS = make_settings()
PAYLOAD = encode_batch(
    NormalizedBatch(
        org_id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        key_id=uuid.uuid4(),
        signal="traces",
        received_us=1,
    )
)


class Dead:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _settings: Any, payload: str, reason: str) -> None:
        self.calls.append((payload, reason))


def _ok(_settings: Any, _batch: Any) -> WriteResult:
    return WriteResult(skipped_duplicate_batch=False, inserted={"spans": 0}, rejected={})


def _fail(_settings: Any, _batch: Any) -> WriteResult:
    raise ConnectionError("db down")


def test_success() -> None:
    dead = Dead()
    out = process_ingest_payload(PAYLOAD, SETTINGS, retries=0, write=_ok, dead=dead)
    assert out["duplicate_batch"] is False
    assert dead.calls == []


@pytest.mark.parametrize("retries", range(INGEST_MAX_RETRIES))
def test_failure_before_last_attempt_requests_a_retry(retries: int) -> None:
    dead = Dead()
    with pytest.raises(ConnectionError):
        process_ingest_payload(PAYLOAD, SETTINGS, retries=retries, write=_fail, dead=dead)
    assert dead.calls == []


def test_last_attempt_dead_letters() -> None:
    dead = Dead()
    out = process_ingest_payload(
        PAYLOAD, SETTINGS, retries=INGEST_MAX_RETRIES, write=_fail, dead=dead
    )
    assert out == {"dead_letter": "ConnectionError"}
    assert dead.calls == [(PAYLOAD, "ConnectionError")]


def test_undecodable_payload_is_dead_lettered_without_retry() -> None:
    dead = Dead()
    out = process_ingest_payload("not-a-batch", SETTINGS, retries=0, write=_ok, dead=dead)
    assert out == {"dead_letter": "undecodable"}
    assert dead.calls == [("not-a-batch", "undecodable")]


def test_retry_countdown_is_exponential_with_jitter() -> None:
    assert retry_countdown(0, rng=lambda: 0.0) == 0.5
    assert retry_countdown(0, rng=lambda: 1.0) == 1.5
    assert retry_countdown(3, rng=lambda: 0.5) == 8.0
    assert retry_countdown(10, rng=lambda: 0.5) == 60.0  # capped
