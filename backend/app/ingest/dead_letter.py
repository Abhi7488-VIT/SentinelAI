"""Capped dead-letter list for ingest batches that could not be written after all retries.

Kept in Redis (`ingest:dead`, newest first, at most INGEST_DEAD_LETTER_MAX entries) so an operator
can inspect and replay them; every entry is also counted in `ingest_dead_letters_total`.
"""

from __future__ import annotations

import json
import time

from redis import Redis

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import INGEST_DEAD_LETTERS

DEAD_LETTER_KEY = "ingest:dead"
logger = get_logger("app.ingest.dead_letter")


def dead_letter(
    settings: Settings, payload: str, reason: str, *, redis: Redis | None = None
) -> None:
    INGEST_DEAD_LETTERS.labels(reason).inc()
    logger.error("ingest_batch_dead_lettered", reason=reason, payload_bytes=len(payload))
    client = redis or Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=2)
    try:
        entry = json.dumps({"reason": reason, "at": time.time(), "payload": payload})
        pipe = client.pipeline()
        pipe.lpush(DEAD_LETTER_KEY, entry)
        pipe.ltrim(DEAD_LETTER_KEY, 0, settings.ingest_dead_letter_max - 1)
        pipe.execute()
    finally:
        if redis is None:
            client.close()
