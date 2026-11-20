"""Exception signatures: type + message with variable parts stripped, hashed.

Used to rank exceptions in a window and (T8) to group detections that fail the same way. The
message is untrusted telemetry: it is only normalized, truncated and hashed here, never
interpreted.
"""

from __future__ import annotations

import hashlib
import re

_PATTERNS = [
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"\b0x[0-9a-fA-F]+\b"),
    re.compile(r"\b[0-9a-fA-F]{8,}\b"),
    re.compile(r"\d+(?:\.\d+)?"),
]
_SPACES = re.compile(r"\s+")
MAX_MESSAGE = 200


def normalize_message(message: str | None) -> str:
    text = (message or "")[:2048]
    for pattern in _PATTERNS:
        text = pattern.sub("#", text)
    return _SPACES.sub(" ", text).strip()[:MAX_MESSAGE]


def signature(exception_type: str | None, message: str | None) -> str:
    """16-hex-char stable id of (type, normalized message)."""
    key = f"{exception_type or ''}\x00{normalize_message(message)}"
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]
