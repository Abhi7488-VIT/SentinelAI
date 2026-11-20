"""Decode an OTLP/HTTP request body into an OTLP protobuf message, with hard size caps.

Supports `application/x-protobuf` (the Collector exporter's default) and `application/json`
(OTLP/JSON: trace/span ids are hex strings), with `Content-Encoding: gzip` or none.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass
from typing import Any, Literal

from google.protobuf import json_format
from google.protobuf.message import DecodeError, Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from app.core.errors import AppError
from app.ingest.batch import Signal

Encoding = Literal["protobuf", "json"]

REQUEST_TYPES: dict[Signal, type[Message]] = {
    "traces": ExportTraceServiceRequest,
    "logs": ExportLogsServiceRequest,
    "metrics": ExportMetricsServiceRequest,
}
_ID_FIELDS = {"traceId", "spanId", "parentSpanId", "trace_id", "span_id", "parent_span_id"}


class MalformedPayloadError(AppError):
    status_code = 400
    code = "malformed_payload"
    message = "The OTLP payload could not be decoded."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "The request body is too large."


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    code = "unsupported_media_type"
    message = "Use application/x-protobuf or application/json, optionally gzip-encoded."


@dataclass(frozen=True, slots=True)
class BodyFormat:
    encoding: Encoding
    gzip: bool


def body_format(content_type: str | None, content_encoding: str | None) -> BodyFormat:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media == "application/x-protobuf":
        encoding: Encoding = "protobuf"
    elif media == "application/json":
        encoding = "json"
    else:
        raise UnsupportedMediaTypeError()
    coding = (content_encoding or "identity").strip().lower()
    if coding not in {"identity", "gzip"}:
        raise UnsupportedMediaTypeError("Unsupported Content-Encoding; use gzip or none.")
    return BodyFormat(encoding=encoding, gzip=coding == "gzip")


def gunzip_capped(data: bytes, max_bytes: int) -> bytes:
    """Decompress gzip, refusing output larger than `max_bytes` (decompression-bomb guard)."""
    decomp = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = decomp.decompress(data, max_bytes + 1)
    except zlib.error as exc:
        raise MalformedPayloadError("Invalid gzip data.") from exc
    if len(out) > max_bytes or decomp.unconsumed_tail:
        raise PayloadTooLargeError("The decompressed body is too large.")
    if not decomp.eof:
        raise MalformedPayloadError("Truncated gzip data.")
    return out


def _hex_ids_to_base64(node: Any) -> Any:
    """OTLP/JSON encodes ids as hex; protobuf's JSON parser expects base64 for bytes fields."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _ID_FIELDS and isinstance(value, str):
                try:
                    out[key] = base64.b64encode(bytes.fromhex(value)).decode("ascii")
                except ValueError as exc:
                    raise MalformedPayloadError(f"{key} is not hex.") from exc
            else:
                out[key] = _hex_ids_to_base64(value)
        return out
    if isinstance(node, list):
        return [_hex_ids_to_base64(v) for v in node]
    return node


def parse(signal: Signal, data: bytes, encoding: Encoding) -> Message:
    message = REQUEST_TYPES[signal]()
    try:
        if encoding == "protobuf":
            message.ParseFromString(data)
        else:
            doc = json.loads(data)
            if not isinstance(doc, dict):
                raise MalformedPayloadError("The JSON body must be an object.")
            json_format.ParseDict(_hex_ids_to_base64(doc), message, ignore_unknown_fields=True)
    except (DecodeError, json_format.ParseError, ValueError, binascii.Error, RecursionError) as exc:
        raise MalformedPayloadError() from exc
    return message


def response_body(message: Message, encoding: Encoding) -> tuple[bytes, str]:
    if encoding == "protobuf":
        return message.SerializeToString(), "application/x-protobuf"
    return json.dumps(json_format.MessageToDict(message)).encode(), "application/json"
