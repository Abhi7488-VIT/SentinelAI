"""OTLP ingest use case: authenticate → limit → decode → validate/normalize → bounded enqueue.

The order puts the cheapest rejections first. The org and service of every stored record come
from the API key (never from the payload), so a key can only ever write into its own service.
CPU-heavy steps (gunzip, protobuf/JSON parsing, normalization) run in a worker thread so one large
request does not stall the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.protobuf.message import Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsPartialSuccess,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsPartialSuccess,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceResponse,
)

from app.core.config import Settings
from app.core.errors import AppError, DependencyUnavailableError, RateLimitedError
from app.core.logging import get_logger
from app.core.metrics import INGEST_QUEUE_PENDING, INGEST_RECORDS, INGEST_REQUESTS
from app.core.rate_limit import RateLimiter
from app.ingest import decode
from app.ingest.auth import InvalidIngestKeyError, KeyResolver
from app.ingest.batch import NormalizedBatch, Signal, encode_batch
from app.ingest.normalize import (
    Window,
    count_records,
    normalize_logs,
    normalize_metrics,
    normalize_traces,
)
from app.ingest.principal import IngestPrincipal
from app.ingest.queue import IngestQueue, IngestUnavailableError

logger = get_logger("app.ingest")

_NORMALIZERS: dict[Signal, Callable[[Any, IngestPrincipal, Window], NormalizedBatch]] = {
    "traces": normalize_traces,
    "logs": normalize_logs,
    "metrics": normalize_metrics,
}


class TooManyRecordsError(AppError):
    status_code = 413
    code = "too_many_records"
    message = "Too many records in one request; send smaller batches."


@dataclass(frozen=True, slots=True)
class IngestResponse:
    body: bytes
    media_type: str
    accepted: int
    rejected: int


def _response(signal: Signal, rejected: int, message: str) -> Message:
    if signal == "traces":
        traces = ExportTraceServiceResponse()
        if rejected:
            traces.partial_success.CopyFrom(
                ExportTracePartialSuccess(rejected_spans=rejected, error_message=message)
            )
        return traces
    if signal == "logs":
        logs = ExportLogsServiceResponse()
        if rejected:
            logs.partial_success.CopyFrom(
                ExportLogsPartialSuccess(rejected_log_records=rejected, error_message=message)
            )
        return logs
    metrics = ExportMetricsServiceResponse()
    if rejected:
        metrics.partial_success.CopyFrom(
            ExportMetricsPartialSuccess(rejected_data_points=rejected, error_message=message)
        )
    return metrics


def _rejection_message(rejected: dict[str, int]) -> str:
    return "rejected: " + ", ".join(f"{k}={v}" for k, v in sorted(rejected.items()))


class IngestService:
    def __init__(
        self,
        settings: Settings,
        resolver: KeyResolver,
        limiter: RateLimiter,
        queue: IngestQueue,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.limiter = limiter
        self.queue = queue
        self.clock = clock

    async def ingest(
        self,
        signal: Signal,
        *,
        authorization: str | None,
        content_type: str | None,
        content_encoding: str | None,
        body: bytes,
    ) -> IngestResponse:
        try:
            principal = await self.resolver.resolve(authorization)
        except InvalidIngestKeyError:
            INGEST_REQUESTS.labels(signal, "unauthorized").inc()
            raise
        try:
            return await self._ingest(signal, principal, content_type, content_encoding, body)
        except RateLimitedError:
            INGEST_REQUESTS.labels(signal, "rate_limited").inc()
            raise
        except IngestUnavailableError:
            INGEST_REQUESTS.labels(signal, "unavailable").inc()
            raise
        except (decode.PayloadTooLargeError, TooManyRecordsError):
            INGEST_REQUESTS.labels(signal, "too_large").inc()
            raise
        except decode.UnsupportedMediaTypeError:
            INGEST_REQUESTS.labels(signal, "unsupported").inc()
            raise

    async def _hit(self, scope: str, principal: IngestPrincipal, limit: int, cost: int) -> None:
        try:
            await self.limiter.hit(
                scope, str(principal.key_id), limit=limit, window_seconds=1, cost=cost
            )
        except DependencyUnavailableError as exc:  # Redis down: tell the sender to retry
            raise IngestUnavailableError("Rate limiter unavailable.") from exc

    async def _ingest(
        self,
        signal: Signal,
        principal: IngestPrincipal,
        content_type: str | None,
        content_encoding: str | None,
        body: bytes,
    ) -> IngestResponse:
        s = self.settings
        await self._hit("ingest_requests", principal, s.ingest_rate_requests_per_second, 1)
        fmt = decode.body_format(content_type, content_encoding)
        now_us = int(self.clock() * 1_000_000)

        def parse() -> tuple[Message, int]:
            data = decode.gunzip_capped(body, s.ingest_max_decompressed_bytes) if fmt.gzip else body
            message = decode.parse(signal, data, fmt.encoding)
            return message, count_records(signal, message)

        try:
            message, n_records = await asyncio.to_thread(parse)
        except decode.MalformedPayloadError:
            INGEST_REQUESTS.labels(signal, "malformed").inc()
            INGEST_RECORDS.labels(signal, "rejected", "malformed_payload").inc()
            await self._record_rejection_only(signal, principal, now_us, "malformed_payload")
            raise

        if n_records > s.ingest_max_records_per_request:
            INGEST_RECORDS.labels(signal, "rejected", "too_many_records").inc(n_records)
            await self._record_rejection_only(
                signal, principal, now_us, "too_many_records", n_records
            )
            raise TooManyRecordsError(
                f"{n_records} records; the limit is {s.ingest_max_records_per_request}."
            )
        await self._hit(
            "ingest_records", principal, s.ingest_rate_records_per_second, max(1, n_records)
        )

        window = Window.around(
            now_us, max_age_s=s.ingest_max_age_seconds, max_future_s=s.ingest_max_future_seconds
        )
        batch = await asyncio.to_thread(_NORMALIZERS[signal], message, principal, window)
        if batch.accepted or batch.rejected:
            await self._enqueue(batch)

        INGEST_REQUESTS.labels(signal, "partial" if batch.rejected else "accepted").inc()
        if batch.accepted:
            INGEST_RECORDS.labels(signal, "accepted", "").inc(batch.accepted)
        for reason, n in batch.rejected.items():
            INGEST_RECORDS.labels(signal, "rejected", reason).inc(n)
        if batch.rejected:
            logger.info(
                "ingest_records_rejected",
                signal=signal,
                service_id=str(principal.service_id),
                rejected=batch.rejected,
            )

        reply = _response(signal, batch.rejected_total, _rejection_message(batch.rejected))
        payload, media_type = decode.response_body(reply, fmt.encoding)
        return IngestResponse(payload, media_type, batch.accepted, batch.rejected_total)

    async def _enqueue(self, batch: NormalizedBatch) -> None:
        payload = await asyncio.to_thread(encode_batch, batch)
        await self.queue.enqueue(payload)
        with contextlib.suppress(Exception):  # metrics sampling must never fail a request
            INGEST_QUEUE_PENDING.set(await self.queue.pending())

    async def _record_rejection_only(
        self, signal: Signal, principal: IngestPrincipal, now_us: int, reason: str, n: int = 1
    ) -> None:
        """Best effort: get a rejection-only batch to the writer so it lands in ingest_usage."""
        batch = NormalizedBatch(
            org_id=principal.org_id,
            service_id=principal.service_id,
            key_id=principal.key_id,
            signal=signal,
            received_us=now_us,
            rejected={reason: n},
        )
        try:
            await self.queue.enqueue(encode_batch(batch))
        except (IngestUnavailableError, decode.PayloadTooLargeError):
            logger.warning("ingest_rejection_not_recorded", reason=reason, signal=signal)
