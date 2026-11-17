"""Opaque keyset cursors."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor


def _b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def test_round_trip() -> None:
    ts = datetime(2026, 11, 17, 10, 0, 0, 123456, tzinfo=UTC)
    id_ = uuid.uuid4()
    cursor = decode_cursor(encode_cursor(ts, id_))
    assert cursor.created_at == ts
    assert cursor.id == str(id_)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "!!!",
        "x" * 300,
        _b64({}),
        _b64([]),
        _b64({"c": "not-a-date", "i": "1"}),
        _b64({"c": "2026-01-01T00:00:00", "i": "1"}),  # naive timestamp
        _b64({"c": "2026-01-01T00:00:00+00:00", "i": 1}),  # id must be a string
    ],
)
def test_invalid_cursors(bad: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(bad)
