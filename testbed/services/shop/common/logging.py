"""Logging: JSON lines on stdout (with trace correlation) plus OTLP export of the same records."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from shop.common.settings import ServiceSettings

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",  # uvicorn's terminal-colour duplicate of msg
}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith(("_", "otel")):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(
    settings: ServiceSettings, extra_handler: logging.Handler | None = None
) -> None:
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonFormatter(settings.service_name))
    root = logging.getLogger()
    root.handlers = [stdout] + ([extra_handler] if extra_handler is not None else [])
    root.setLevel(settings.log_level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # One line per outgoing request is noise; traces carry the client calls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Never feed the exporter's own warnings back into the exporter.
    otel = logging.getLogger("opentelemetry")
    otel.propagate = False
    otel.handlers = [stdout]
    otel.setLevel(logging.WARNING)
