"""Short-lived response cache for catalogue reads, keyed by the request URL."""

from __future__ import annotations

import time
from typing import Any


class ResponseCache:
    def __init__(self, ttl_s: float = 30.0) -> None:
        self.ttl_s = ttl_s
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None or entry[0] < time.monotonic():
            return None
        return entry[1]

    def put(self, key: str, payload: Any) -> None:
        # Expired entries are overwritten when their URL is requested again.
        self._entries[key] = (time.monotonic() + self.ttl_s, payload)

    def __len__(self) -> int:
        return len(self._entries)
