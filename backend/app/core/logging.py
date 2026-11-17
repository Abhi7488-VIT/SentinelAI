"""Structured JSON logging for the API and worker.

All stdlib loggers (uvicorn, sqlalchemy, celery) are routed through the same structlog
formatter so every line on stdout is one JSON object carrying the bound request context.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|authorization|cookie|api[_-]?key|dsn|credential", re.IGNORECASE
)


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(key, v) for v in value]
    return value


def redact_secrets(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Replace values whose key looks sensitive, recursively. Runs last before rendering."""
    for key in list(event_dict.keys()):
        if key == "event":
            continue
        event_dict[key] = _redact_value(key, event_dict[key])
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def configure_logging(level: str = "INFO") -> None:
    shared = _shared_processors()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn: keep error logs (propagated to root, JSON); drop its access log because
    # RequestContextMiddleware emits a richer one with request_id and duration.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def redact_mapping(data: MutableMapping[str, Any]) -> dict[str, Any]:
    """Redact a plain dict (used for audit metadata before persisting)."""
    return {k: _redact_value(k, v) for k, v in data.items()}
