"""OTLP/HTTP ingest API (ingest process): auth, limits, validation, partial success, backpressure.

The writer (T4) is not involved: a recording queue captures the normalized batches, except in the
test that drives the real Celery/Redis queue to prove the 503 bound.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from celery import Celery
from fastapi import FastAPI
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from prometheus_client import REGISTRY
from redis.asyncio import Redis

from app.ingest.batch import NormalizedBatch, decode_batch
from app.ingest.queue import CeleryIngestQueue, IngestUnavailableError
from app.ingest_main import create_ingest_app
from app.worker.config import configure_celery
from tests import otlp_factory as f
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.integration.factories import (
    Actor,
    create_api_key,
    create_project,
    create_service,
    register,
)

PB = {"Content-Type": "application/x-protobuf"}
TRACES = "/api/v1/otlp/v1/traces"
LOGS = "/api/v1/otlp/v1/logs"
METRICS = "/api/v1/otlp/v1/metrics"


class RecordingQueue:
    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.full = False

    async def pending(self) -> int:
        return len(self.payloads)

    async def enqueue(self, payload: str) -> None:
        if self.full:
            raise IngestUnavailableError("The ingest queue is full.", code="ingest_queue_full")
        self.payloads.append(payload)

    @property
    def batches(self) -> list[NormalizedBatch]:
        return [decode_batch(p) for p in self.payloads]


IngestFactory = Callable[..., Awaitable[tuple[httpx.AsyncClient, RecordingQueue]]]


@pytest.fixture
async def make_ingest(engine: Any) -> AsyncIterator[IngestFactory]:
    stack: list[tuple[LifespanManager, httpx.AsyncClient]] = []

    async def factory(**overrides: Any) -> tuple[httpx.AsyncClient, RecordingQueue]:
        settings = make_settings(ingest_key_cache_seconds=0, **overrides)
        queue = RecordingQueue()
        app: FastAPI = create_ingest_app(settings, queue=queue)
        manager = LifespanManager(app)
        await manager.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://ingest",
        )
        stack.append((manager, client))
        return client, queue

    yield factory
    for manager, client in stack:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.fixture
async def sender(client: httpx.AsyncClient) -> dict[str, Any]:
    """A registered service with an ingest key (created through the real API)."""
    alice = await register(client, "Alice")
    project = await create_project(client, alice, alice.org_id, "Shop")
    service = await create_service(client, alice, alice.org_id, project["id"], "order-service")
    key = await create_api_key(client, alice, alice.org_id, service["id"])
    return {
        "actor": alice,
        "project": project,
        "service": service,
        "key": key,
        "auth": {"Authorization": f"Bearer {key['key']}"},
    }


def _counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ---- happy paths -------------------------------------------------------------------------------


async def test_protobuf_traces_are_normalized_and_enqueued(
    make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, queue = await make_ingest()
    req = f.traces_request("order-service", [f.span(), f.span(error=True)])
    resp = await ingest.post(
        TRACES, content=req.SerializeToString(), headers={**PB, **sender["auth"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/x-protobuf"
    reply = ExportTraceServiceResponse.FromString(resp.content)
    assert not reply.HasField("partial_success")
    [batch] = queue.batches
    assert str(batch.org_id) == sender["actor"].org_id
    assert str(batch.service_id) == sender["service"]["id"]
    assert str(batch.key_id) == sender["key"]["id"]
    assert batch.signal == "traces"
    assert len(batch.spans) == 2
    assert batch.rejected == {}


async def test_gzip_json_logs(make_ingest: IngestFactory, sender: dict[str, Any]) -> None:
    ingest, queue = await make_ingest()
    doc = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "order-service"}}
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(f.now_ns()),
                                "severityNumber": 17,
                                "body": {
                                    "stringValue": "Timed out waiting for a database connection"
                                },
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    resp = await ingest.post(
        LOGS,
        content=gzip.compress(json.dumps(doc).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip", **sender["auth"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {}
    [batch] = queue.batches
    [log] = batch.logs
    assert log.trace_id is not None
    assert log.trace_id.hex() == "5b8efff798038103d269b633813fc60c"


async def test_metrics(make_ingest: IngestFactory, sender: dict[str, Any]) -> None:
    ingest, queue = await make_ingest()
    req = f.metrics_request(
        "order-service",
        [
            f.gauge("db.client.connection.pending_requests", 3.0),
            f.histogram("h", counts=[1, 2], bounds=[0.5]),
        ],
    )
    resp = await ingest.post(
        METRICS, content=req.SerializeToString(), headers={**PB, **sender["auth"]}
    )
    assert resp.status_code == 200
    assert [m.name for m in queue.batches[0].metrics] == [
        "db.client.connection.pending_requests",
        "h",
    ]


async def test_empty_request_is_ok_and_enqueues_nothing(
    make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, queue = await make_ingest()
    resp = await ingest.post(TRACES, content=b"", headers={**PB, **sender["auth"]})
    assert resp.status_code == 200
    assert queue.payloads == []


# ---- validation: partial success, malformed, wrong service -------------------------------------


async def test_partial_success_counts_rejections(
    make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, queue = await make_ingest()
    before = _counter(
        "ingest_records_total", signal="traces", outcome="rejected", reason="invalid_trace_id"
    )
    req = f.traces_request(
        "order-service", [f.span(), f.span(trace_id=b"\x00" * 16), f.span(name="")]
    )
    resp = await ingest.post(
        TRACES, content=req.SerializeToString(), headers={**PB, **sender["auth"]}
    )
    assert resp.status_code == 200
    reply = ExportTraceServiceResponse.FromString(resp.content)
    assert reply.partial_success.rejected_spans == 2
    assert "invalid_trace_id=1" in reply.partial_success.error_message
    [batch] = queue.batches
    assert len(batch.spans) == 1
    assert batch.rejected == {"invalid_trace_id": 1, "missing_name": 1}
    after = _counter(
        "ingest_records_total", signal="traces", outcome="rejected", reason="invalid_trace_id"
    )
    assert after == before + 1


@pytest.mark.parametrize(
    ("content", "ctype"),
    [
        (b"\xff\xff\xff\xff", "application/x-protobuf"),
        (b"{broken", "application/json"),
        (
            b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "nothex"}]}]}]}',
            "application/json",
        ),
    ],
)
async def test_malformed_payload_is_400_and_counted(
    make_ingest: IngestFactory, sender: dict[str, Any], content: bytes, ctype: str
) -> None:
    ingest, queue = await make_ingest()
    before = _counter("ingest_requests_total", signal="traces", outcome="malformed")
    resp = await ingest.post(
        TRACES, content=content, headers={"Content-Type": ctype, **sender["auth"]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "malformed_payload"
    assert _counter("ingest_requests_total", signal="traces", outcome="malformed") == before + 1
    # a rejection-only batch carries the count to ingest_usage (written by T4)
    [batch] = queue.batches
    assert batch.rejected == {"malformed_payload": 1}
    assert batch.spans == []

    # the process keeps serving
    ok = await ingest.post(
        TRACES,
        content=f.traces_request("order-service", [f.span()]).SerializeToString(),
        headers={**PB, **sender["auth"]},
    )
    assert ok.status_code == 200


async def test_key_cannot_write_into_another_service_or_tenant(
    client: httpx.AsyncClient, make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    """Bob's key, sending telemetry that claims to be Alice's service, writes nothing of Alice's."""
    bob = await register(client, "Bob")
    bob_project = await create_project(client, bob, bob.org_id, "B")
    bob_svc = await create_service(client, bob, bob.org_id, bob_project["id"], "billing")
    bob_key = await create_api_key(client, bob, bob.org_id, bob_svc["id"])
    ingest, queue = await make_ingest()
    req = f.traces_request("order-service", [f.span(attributes={"org_id": sender["actor"].org_id})])
    resp = await ingest.post(
        TRACES,
        content=req.SerializeToString(),
        headers={**PB, "Authorization": f"Bearer {bob_key['key']}"},
    )
    assert resp.status_code == 200
    assert ExportTraceServiceResponse.FromString(resp.content).partial_success.rejected_spans == 1
    [batch] = queue.batches
    assert batch.spans == []
    assert batch.rejected == {"service_mismatch": 1}
    assert str(batch.org_id) == bob.org_id
    assert str(batch.service_id) == bob_svc["id"]


# ---- auth --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth",
    [
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": "Bearer sai_0123456789ab_" + "A" * 43},
    ],
)
async def test_unauthenticated_is_401(
    make_ingest: IngestFactory, sender: dict[str, Any], auth: dict[str, str]
) -> None:
    ingest, queue = await make_ingest()
    resp = await ingest.post(TRACES, content=b"", headers={**PB, **auth})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"
    assert queue.payloads == []


async def test_revoked_key_and_archived_service_are_401(
    client: httpx.AsyncClient, make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, _ = await make_ingest()  # key cache disabled (ttl 0) in these tests
    alice: Actor = sender["actor"]
    base = f"/api/v1/orgs/{alice.org_id}/services/{sender['service']['id']}"
    ok = await ingest.post(TRACES, content=b"", headers={**PB, **sender["auth"]})
    assert ok.status_code == 200
    revoke = await client.post(
        f"{base}/api-keys/{sender['key']['id']}/revoke", headers=alice.headers
    )
    assert revoke.status_code == 200
    assert (
        await ingest.post(TRACES, content=b"", headers={**PB, **sender["auth"]})
    ).status_code == 401

    second = await create_api_key(client, alice, alice.org_id, sender["service"]["id"], "second")
    auth2 = {"Authorization": f"Bearer {second['key']}"}
    assert (await ingest.post(TRACES, content=b"", headers={**PB, **auth2})).status_code == 200
    await client.delete(base, headers=alice.headers)  # archive service (also revokes keys)
    assert (await ingest.post(TRACES, content=b"", headers={**PB, **auth2})).status_code == 401


# ---- limits and backpressure -------------------------------------------------------------------


async def test_request_rate_limit_is_429_with_retry_after(
    make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, _ = await make_ingest(ingest_rate_requests_per_second=3)
    codes = [
        (await ingest.post(TRACES, content=b"", headers={**PB, **sender["auth"]})).status_code
        for _ in range(8)
    ]
    assert codes.count(200) >= 3
    assert 429 in codes
    resp = await ingest.post(TRACES, content=b"", headers={**PB, **sender["auth"]})
    if resp.status_code == 429:
        assert int(resp.headers["retry-after"]) >= 1
        assert resp.json()["error"]["code"] == "rate_limited"


async def test_record_rate_limit(make_ingest: IngestFactory, sender: dict[str, Any]) -> None:
    ingest, queue = await make_ingest(ingest_rate_records_per_second=5)
    req = f.traces_request("order-service", [f.span() for _ in range(10)])
    resp = await ingest.post(
        TRACES, content=req.SerializeToString(), headers={**PB, **sender["auth"]}
    )
    assert resp.status_code == 429
    assert "retry-after" in resp.headers
    assert queue.payloads == []


async def test_too_many_records_is_413(make_ingest: IngestFactory, sender: dict[str, Any]) -> None:
    ingest, queue = await make_ingest(ingest_max_records_per_request=3)
    req = f.traces_request("order-service", [f.span() for _ in range(4)])
    resp = await ingest.post(
        TRACES, content=req.SerializeToString(), headers={**PB, **sender["auth"]}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "too_many_records"
    assert queue.batches[0].rejected == {"too_many_records": 4}


async def test_body_and_decompressed_size_caps(
    make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, _ = await make_ingest(ingest_max_body_bytes=2048, ingest_max_decompressed_bytes=4096)
    big = await ingest.post(TRACES, content=b"\0" * 4096, headers={**PB, **sender["auth"]})
    assert big.status_code == 413
    bomb = gzip.compress(b"\0" * (1024 * 1024))
    assert len(bomb) < 2048
    resp = await ingest.post(
        TRACES, content=bomb, headers={**PB, "Content-Encoding": "gzip", **sender["auth"]}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_unsupported_media_type(make_ingest: IngestFactory, sender: dict[str, Any]) -> None:
    ingest, _ = await make_ingest()
    resp = await ingest.post(
        TRACES, content=b"x", headers={"Content-Type": "text/plain", **sender["auth"]}
    )
    assert resp.status_code == 415


async def test_full_queue_is_503_with_retry_after(
    make_ingest: IngestFactory, sender: dict[str, Any]
) -> None:
    ingest, queue = await make_ingest()
    queue.full = True
    req = f.traces_request("order-service", [f.span()])
    resp = await ingest.post(
        TRACES, content=req.SerializeToString(), headers={**PB, **sender["auth"]}
    )
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    assert resp.json()["error"]["code"] == "ingest_queue_full"


async def test_celery_queue_bound_against_real_redis(redis_client: Redis) -> None:
    """The real queue: messages land in the broker list; past max_pending it refuses (503)."""
    settings = make_settings(celery_broker_url=TEST_REDIS_URL)
    celery = configure_celery(Celery("ingest-test"), settings)
    queue = CeleryIngestQueue(
        celery, redis_client, max_pending=2, max_message_bytes=1024, queue="ingest-test"
    )
    await queue.enqueue("a")
    await queue.enqueue("b")
    assert await redis_client.llen("ingest-test") == 2
    assert await queue.pending() == 2
    with pytest.raises(IngestUnavailableError) as exc:
        await queue.enqueue("c")
    assert exc.value.headers == {"Retry-After": "5"}
    from app.ingest.decode import PayloadTooLargeError

    with pytest.raises(PayloadTooLargeError):
        await queue.enqueue("x" * 2000)
    assert await redis_client.llen("ingest-test") == 2
