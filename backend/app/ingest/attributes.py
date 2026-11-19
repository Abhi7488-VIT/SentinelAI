"""OTLP attribute handling: AnyValue → Python, bounded storage form, secret redaction.

Telemetry is untrusted data (CLAUDE.md invariant 3). Stored attributes are bounded so one noisy
sender cannot grow storage without limit, and values under secret-looking keys are redacted at
ingest so they never reach storage, the UI or (later) a model prompt.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from typing import Any

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

MAX_ATTRIBUTES = 32
MAX_KEY_CHARS = 128
MAX_VALUE_CHARS = 256
MAX_ARRAY_ITEMS = 16
REDACTED = "[redacted]"
_SECRET_KEY_RE = re.compile(
    r"authorization|cookie|passw(or)?d|secret|token|api[-_.]?key|credential|private[-_.]?key",
    re.IGNORECASE,
)


def any_value(value: AnyValue, *, depth: int = 0) -> Any:
    """Convert an OTLP AnyValue to a JSON-compatible Python value (unbounded; see `bounded`)."""
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return value.bool_value
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        d = value.double_value
        return d if math.isfinite(d) else str(d)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    if depth >= 3:  # nested structures are truncated, not recursed indefinitely
        return "[nested]"
    if kind == "array_value":
        return [any_value(v, depth=depth + 1) for v in value.array_value.values[:MAX_ARRAY_ITEMS]]
    if kind == "kvlist_value":
        return {kv.key: any_value(kv.value, depth=depth + 1) for kv in value.kvlist_value.values}
    return None


def to_dict(attrs: Iterable[KeyValue]) -> dict[str, Any]:
    """Full attribute map (later keys win). Used to extract known fields before bounding."""
    return {kv.key: any_value(kv.value) for kv in attrs}


def is_secret_key(key: str) -> bool:
    return _SECRET_KEY_RE.search(key) is not None


def _bounded_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, bool | int | float) or value is None:
        return value
    # arrays / maps are stored as capped JSON text
    return json.dumps(value, separators=(",", ":"), default=str)[:MAX_VALUE_CHARS]


def bounded(attrs: dict[str, Any]) -> dict[str, Any]:
    """Storage form: ≤ 32 keys (sorted, deterministic), capped keys/values, secrets redacted."""
    out: dict[str, Any] = {}
    for key in sorted(attrs)[:MAX_ATTRIBUTES]:
        short = key[:MAX_KEY_CHARS]
        out[short] = REDACTED if is_secret_key(key) else _bounded_value(attrs[key])
    return out


def text(value: Any, limit: int) -> str | None:
    """A scalar attribute as bounded text, or None."""
    if value is None:
        return None
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s[:limit] if s else None


def integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
