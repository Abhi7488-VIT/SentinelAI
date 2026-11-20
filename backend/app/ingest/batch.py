"""Normalized ingest batch: the message the ingest API enqueues and the writer (T4) consumes.

One batch = one OTLP request after validation, for exactly one (org, service, key). The org and
service come from the authenticated key, never from the payload. Timestamps are integer
microseconds since the epoch (Postgres `timestamptz` resolution); ids are raw bytes.

Wire form: JSON (bytes as hex) → gzip → base64 text, so it fits Celery's JSON serializer.
"""

from __future__ import annotations

import base64
import dataclasses
import gzip
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Signal = Literal["traces", "logs", "metrics"]
BATCH_VERSION = 1


@dataclass(slots=True)
class SpanRow:
    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None
    name: str
    kind: int
    start_us: int
    end_us: int
    duration_ms: float
    status_code: int
    status_message: str | None
    http_method: str | None
    http_route: str | None
    http_status_code: int | None
    db_system: str | None
    server_address: str | None
    peer_service: str | None
    exception_type: str | None
    exception_message: str | None
    service_instance_id: str | None
    service_version: str | None
    attributes: dict[str, Any]


@dataclass(slots=True)
class LogRow:
    record_hash: bytes
    ts_us: int
    observed_us: int | None
    severity_number: int
    severity_text: str | None
    body: str | None
    trace_id: bytes | None
    span_id: bytes | None
    event_name: str | None
    exception_type: str | None
    exception_message: str | None
    exception_stacktrace: str | None
    service_instance_id: str | None
    service_version: str | None
    attributes: dict[str, Any]


@dataclass(slots=True)
class MetricRow:
    series_hash: bytes
    name: str
    unit: str | None
    kind: int
    is_monotonic: bool
    temporality: int
    series_attributes: dict[str, Any]
    ts_us: int
    start_us: int | None
    value: float | None
    count: int | None
    sum: float | None
    bucket_counts: list[int] | None
    explicit_bounds: list[float] | None


@dataclass(slots=True)
class DeploymentRow:
    deployment_id: str
    status: str
    version: str | None
    previous_version: str | None
    ts_us: int


@dataclass(slots=True)
class NormalizedBatch:
    """`batch_id` makes processing exactly-once: the writer records it in the same transaction."""

    org_id: uuid.UUID
    service_id: uuid.UUID
    key_id: uuid.UUID
    signal: Signal
    received_us: int
    spans: list[SpanRow] = field(default_factory=list)
    logs: list[LogRow] = field(default_factory=list)
    metrics: list[MetricRow] = field(default_factory=list)
    deployments: list[DeploymentRow] = field(default_factory=list)
    # Records dropped during validation, by reason (the "counted" in "counted and dropped").
    rejected: dict[str, int] = field(default_factory=dict)
    batch_id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def accepted(self) -> int:
        return len(self.spans) + len(self.logs) + len(self.metrics) + len(self.deployments)

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())

    def reject(self, reason: str, n: int = 1) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + n


_BYTES_FIELDS = {"trace_id", "span_id", "parent_span_id", "record_hash", "series_hash"}


def _default(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"not serializable: {type(value).__name__}")


def encode_batch(batch: NormalizedBatch) -> str:
    doc = {"v": BATCH_VERSION, **dataclasses.asdict(batch)}
    raw = json.dumps(doc, default=_default, separators=(",", ":")).encode()
    return base64.b64encode(gzip.compress(raw, compresslevel=1)).decode("ascii")


def _row[R](cls: type[R], data: dict[str, Any]) -> R:
    for name in _BYTES_FIELDS & data.keys():
        if data[name] is not None:
            data[name] = bytes.fromhex(data[name])
    return cls(**data)


def decode_batch(payload: str) -> NormalizedBatch:
    doc = json.loads(gzip.decompress(base64.b64decode(payload)))
    if doc.pop("v") != BATCH_VERSION:
        raise ValueError("unsupported batch version")
    return NormalizedBatch(
        org_id=uuid.UUID(doc["org_id"]),
        service_id=uuid.UUID(doc["service_id"]),
        key_id=uuid.UUID(doc["key_id"]),
        signal=doc["signal"],
        received_us=doc["received_us"],
        spans=[_row(SpanRow, r) for r in doc["spans"]],
        logs=[_row(LogRow, r) for r in doc["logs"]],
        metrics=[_row(MetricRow, r) for r in doc["metrics"]],
        deployments=[_row(DeploymentRow, r) for r in doc["deployments"]],
        rejected=dict(doc["rejected"]),
        batch_id=uuid.UUID(doc["batch_id"]),
    )
