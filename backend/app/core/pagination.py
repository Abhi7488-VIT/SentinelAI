"""Opaque keyset cursors over (created_at DESC, id DESC)."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from app.core.errors import AppError

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


class InvalidCursorError(AppError):
    status_code = 422
    code = "invalid_cursor"
    message = "The pagination cursor is invalid."


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: str


def encode_cursor(created_at: datetime, id_: object) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": str(id_)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> Cursor:
    if len(cursor) > 256:
        raise InvalidCursorError()
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        created_at = datetime.fromisoformat(data["c"])
        id_ = data["i"]
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidCursorError() from exc
    if not isinstance(id_, str) or created_at.tzinfo is None:
        raise InvalidCursorError()
    return Cursor(created_at=created_at, id=id_)
