"""Ingest decoding, attribute bounding, normalization and the batch wire format (no services)."""

from __future__ import annotations

import gzip
import json
import math
import time
import uuid

import pytest
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    ExponentialHistogram,
    ExponentialHistogramDataPoint,
    Metric,
)

from app.ingest import attributes as attrs
from app.ingest import decode
from app.ingest.batch import decode_batch, encode_batch
from app.ingest.normalize import (
    Window,
    count_records,
    normalize_logs,
    normalize_metrics,
    normalize_traces,
)
from app.ingest.principal import IngestPrincipal
from tests import otlp_factory as f

P = IngestPrincipal(
    key_id=uuid.uuid4(), org_id=uuid.uuid4(), service_id=uuid.uuid4(), service_name="order-service"
)


def _window() -> Window:
    return Window.around(time.time_ns() // 1000, max_age_s=7200, max_future_s=300)


# ---- decode ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctype", "cenc", "expected"),
    [
        ("application/x-protobuf", None, ("protobuf", False)),
        ("application/json; charset=utf-8", "gzip", ("json", True)),
        ("Application/X-Protobuf", "identity", ("protobuf", False)),
    ],
)
def test_body_format(ctype: str, cenc: str | None, expected: tuple[str, bool]) -> None:
    fmt = decode.body_format(ctype, cenc)
    assert (fmt.encoding, fmt.gzip) == expected


@pytest.mark.parametrize(
    ("ctype", "cenc"), [("text/plain", None), (None, None), ("application/json", "br")]
)
def test_body_format_rejects(ctype: str | None, cenc: str | None) -> None:
    with pytest.raises(decode.UnsupportedMediaTypeError):
        decode.body_format(ctype, cenc)


def test_gunzip_bomb_is_refused() -> None:
    bomb = gzip.compress(b"\0" * (20 * 1024 * 1024))
    assert len(bomb) < 64 * 1024
    with pytest.raises(decode.PayloadTooLargeError):
        decode.gunzip_capped(bomb, 1024 * 1024)


def test_gunzip_truncated_and_invalid() -> None:
    good = gzip.compress(b"x" * 1000)
    assert decode.gunzip_capped(good, 2000) == b"x" * 1000
    with pytest.raises(decode.MalformedPayloadError):
        decode.gunzip_capped(good[:-10], 2000)
    with pytest.raises(decode.MalformedPayloadError):
        decode.gunzip_capped(b"not gzip", 2000)


@pytest.mark.parametrize(
    ("encoding", "data"),
    [
        ("protobuf", b"\xff\xff\xff\xff"),
        ("json", b"{not json"),
        ("json", b"[1, 2]"),
        ("json", b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz"}]}]}]}'),
        ("json", b"\xff\xfe"),
    ],
)
def test_parse_malformed(encoding: decode.Encoding, data: bytes) -> None:
    with pytest.raises(decode.MalformedPayloadError):
        decode.parse("traces", data, encoding)


def test_parse_json_hex_ids() -> None:
    trace_hex, span_hex = "5b8efff798038103d269b633813fc60c", "eee19b7ec3c1b174"
    doc = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "order-service"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_hex,
                                "spanId": span_hex,
                                "name": "GET /x",
                                "kind": 2,
                                "startTimeUnixNano": str(time.time_ns()),
                                "endTimeUnixNano": str(time.time_ns() + 1000),
                            }
                        ]
                    }
                ],
            }
        ]
    }
    message = decode.parse("traces", json.dumps(doc).encode(), "json")
    batch = normalize_traces(message, P, _window())  # type: ignore[arg-type]
    assert [s.trace_id.hex() for s in batch.spans] == [trace_hex]
    assert batch.spans[0].span_id.hex() == span_hex


# ---- attributes --------------------------------------------------------------------------------


def test_bounded_attributes_caps_and_redacts() -> None:
    raw: dict[str, object] = {f"k{i:02d}": "v" * 1000 for i in range(40)}
    raw["http.request.header.authorization"] = "Bearer abc"
    raw["db.password"] = "hunter2"
    raw["x" * 300] = 1
    out = attrs.bounded(raw)
    assert len(out) == 32
    assert all(len(k) <= 128 for k in out)
    assert all(len(v) <= 256 for v in out.values() if isinstance(v, str))
    small = attrs.bounded({"http.request.header.authorization": "Bearer abc", "db.password": "p"})
    assert small == {"db.password": "[redacted]", "http.request.header.authorization": "[redacted]"}


def test_any_value_handles_nesting_and_non_finite() -> None:
    req = f.traces_request("order-service", [f.span(attributes={"ratio": math.inf})])
    span = req.resource_spans[0].scope_spans[0].spans[0]
    assert attrs.to_dict(span.attributes) == {"ratio": "inf"}


# ---- traces ------------------------------------------------------------------------------------


def test_normalize_traces_extracts_fields() -> None:
    parent = b"\x01" * 8
    req = f.traces_request(
        "order-service",
        [
            f.span(
                parent_span_id=parent,
                error=True,
                attributes={
                    "http.request.method": "GET",
                    "http.route": "/orders",
                    "http.response.status_code": 503,
                    "db.system.name": "postgresql",
                    "server.address": "postgres",
                    "user.token": "secret-value",
                },
                exception=("TimeoutError", "QueuePool limit of size 10 overflow 0 reached"),
            )
        ],
        instance="i-7",
        version="2.3.0",
    )
    batch = normalize_traces(req, P, _window())
    assert batch.rejected == {}
    [row] = batch.spans
    assert (row.http_method, row.http_route, row.http_status_code) == ("GET", "/orders", 503)
    assert (row.db_system, row.server_address) == ("postgresql", "postgres")
    assert row.status_code == 2
    assert row.parent_span_id == parent
    assert row.exception_type == "TimeoutError"
    assert row.exception_message is not None
    assert "QueuePool" in row.exception_message
    assert (row.service_instance_id, row.service_version) == ("i-7", "2.3.0")
    assert row.attributes["user.token"] == "[redacted]"
    assert row.duration_ms == pytest.approx(5.0)
    assert (batch.org_id, batch.service_id) == (P.org_id, P.service_id)


def test_normalize_traces_rejections_are_counted_not_raised() -> None:
    now = time.time_ns()
    spans = [
        f.span(),  # ok
        f.span(trace_id=b"\x00" * 16),
        f.span(trace_id=b"\x01" * 15),
        f.span(span_id=b"\x00" * 8),
        f.span(parent_span_id=b"\x01" * 3),
        f.span(name=""),
        f.span(start_ns=now - 3 * 3600 * 10**9),
        f.span(start_ns=now + 3600 * 10**9),
        f.span(start_ns=now, duration_ns=-5),
    ]
    batch = normalize_traces(f.traces_request("order-service", spans), P, _window())
    assert len(batch.spans) == 1
    assert batch.rejected == {
        "invalid_trace_id": 2,
        "invalid_span_id": 1,
        "invalid_parent_span_id": 1,
        "missing_name": 1,
        "too_old": 1,
        "in_future": 1,
        "invalid_timestamp": 1,
    }


def test_spans_of_another_service_are_rejected() -> None:
    req = f.traces_request("payment-service", [f.span(), f.span()])
    batch = normalize_traces(req, P, _window())
    assert batch.spans == []
    assert batch.rejected == {"service_mismatch": 2}
    assert count_records("traces", req) == 2


# ---- logs --------------------------------------------------------------------------------------


def test_normalize_logs_and_dedup_hash() -> None:
    ts = time.time_ns()
    trace = b"\x02" * 16
    records = [
        f.log_record(
            "Timed out waiting for a database connection", ts_ns=ts, severity=17, trace_id=trace
        ),
        f.log_record(
            "Timed out waiting for a database connection", ts_ns=ts, severity=17, trace_id=trace
        ),
        f.log_record("x" * 20_000, ts_ns=ts),
        f.log_record("bad", trace_id=b"\x01" * 5),
    ]
    batch = normalize_logs(f.logs_request("order-service", records), P, _window())
    assert batch.rejected == {"invalid_trace_id": 1}
    a, b, long = batch.logs
    assert a.record_hash == b.record_hash  # identical re-send collapses on insert
    assert a.trace_id == trace
    assert a.severity_number == 17
    assert long.body is not None
    assert len(long.body) == 8192


def test_deployment_events_are_change_records_for_their_target_service() -> None:
    deploy = f.log_record(
        "deployment finished",
        attributes={
            "event.name": "deployment",
            "deployment.id": "abc123",
            "deployment.service.name": "order-service",
            "deployment.version": "1.2.1",
            "deployment.previous_version": "1.2.0",
            "deployment.status": "succeeded",
        },
    )
    other = f.log_record(
        "deployment started",
        attributes={
            "event.name": "deployment",
            "deployment.id": "zzz",
            "deployment.service.name": "payment-service",
        },
    )
    ordinary = f.log_record("hello from the pipeline")
    req = f.logs_request("deploy-pipeline", [deploy, other, ordinary])
    batch = normalize_logs(req, P, _window())
    assert batch.logs == []  # the pipeline's own logs do not belong to order-service
    [d] = batch.deployments
    assert (d.deployment_id, d.status, d.version, d.previous_version) == (
        "abc123",
        "succeeded",
        "1.2.1",
        "1.2.0",
    )
    assert batch.rejected == {"service_mismatch": 2}


# ---- metrics -----------------------------------------------------------------------------------


def test_normalize_metrics_kinds_and_series_identity() -> None:
    start = time.time_ns() - 10**9
    metrics = [
        f.gauge("db.client.connection.count", 10.0, attrs={"db.client.connection.state": "used"}),
        f.gauge("db.client.connection.count", 0.0, attrs={"db.client.connection.state": "idle"}),
        f.counter("shop.orders.created", 42, start_ns=start),
        f.histogram("http.server.request.duration", counts=[1, 2, 3], bounds=[0.1, 0.5]),
        f.histogram("bad.histogram", counts=[1, 2, 3], bounds=[0.1]),
        f.gauge("nan.gauge", math.nan),
        Metric(
            name="exp",
            exponential_histogram=ExponentialHistogram(
                data_points=[ExponentialHistogramDataPoint(time_unix_nano=time.time_ns())]
            ),
        ),
    ]
    req = f.metrics_request("order-service", metrics, instance="i-1")
    batch = normalize_metrics(req, P, _window())
    assert batch.rejected == {"invalid_value": 2, "unsupported_metric_type": 1}
    used, idle, created, hist = batch.metrics
    assert used.series_hash != idle.series_hash
    assert used.series_attributes == {
        "db.client.connection.state": "used",
        "service.instance.id": "i-1",
    }
    assert (created.kind, created.is_monotonic, created.temporality, created.value) == (
        2,
        True,
        2,
        42.0,
    )
    assert created.start_us == start // 1000
    assert (hist.kind, hist.count, hist.bucket_counts, hist.explicit_bounds) == (
        3,
        6,
        [1, 2, 3],
        [0.1, 0.5],
    )

    # same series from a later export → same identity; another instance → a new series
    again = normalize_metrics(req, P, _window()).metrics[0]
    assert again.series_hash == used.series_hash
    other = normalize_metrics(
        f.metrics_request("order-service", metrics[:1], instance="i-2"), P, _window()
    ).metrics[0]
    assert other.series_hash != used.series_hash


# ---- wire format -------------------------------------------------------------------------------


def test_batch_roundtrip() -> None:
    req = f.traces_request("order-service", [f.span(parent_span_id=b"\x03" * 8), f.span(name="")])
    batch = normalize_traces(req, P, _window())
    logs = normalize_logs(f.logs_request("order-service", [f.log_record()]), P, _window())
    metrics = normalize_metrics(
        f.metrics_request("order-service", [f.histogram("h", counts=[1, 1], bounds=[1.0])]),
        P,
        _window(),
    )
    for original in (batch, logs, metrics):
        decoded = decode_batch(encode_batch(original))
        assert decoded == original
    assert batch.rejected == {"missing_name": 1}
