"""Bounded hand-off from the ingest API to the writer (Celery queue `ingest`).

Bound = at most `max_pending` messages waiting in the broker times at most `max_message_bytes` per
message. The pending count is read from the broker list itself (LLEN), so it heals on its own if a
writer dies mid-message — there is no separate counter that could drift. Check-then-send is not
atomic: concurrent requests may overshoot `max_pending` by at most the number of in-flight
requests, which is small and bounded by uvicorn's concurrency.

When the queue is full the API answers 503 with Retry-After; OTLP exporters (incl. the OTel
Collector) back off and retry, so overload slows senders instead of growing memory.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from celery import Celery
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import AppError
from app.ingest.decode import PayloadTooLargeError

INGEST_QUEUE = "ingest"
WRITE_TASK = "ingest.write_batch"
RETRY_AFTER_SECONDS = 5
# kombu's Redis transport keeps one list per priority step; un-prioritized messages use the base.
_PRIORITY_SUFFIXES = ("", "\x06\x163", "\x06\x166", "\x06\x169")


class IngestUnavailableError(AppError):
    status_code = 503
    code = "ingest_unavailable"
    message = "Ingestion is temporarily unavailable; retry later."

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(message, code=code, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


class IngestQueue(Protocol):
    async def pending(self) -> int: ...

    async def enqueue(self, payload: str) -> None:
        """Hand one encoded batch to the writer or raise (413 / 503)."""


class CeleryIngestQueue:
    def __init__(
        self,
        celery: Celery,
        broker: Redis,
        *,
        max_pending: int,
        max_message_bytes: int,
        queue: str = INGEST_QUEUE,
    ) -> None:
        self._celery = celery
        self._broker = broker
        self._max_pending = max_pending
        self._max_message_bytes = max_message_bytes
        self._queue = queue

    async def pending(self) -> int:
        pipe = self._broker.pipeline(transaction=False)
        for suffix in _PRIORITY_SUFFIXES:
            pipe.llen(self._queue + suffix)
        return sum(int(n) for n in await pipe.execute())

    async def enqueue(self, payload: str) -> None:
        if len(payload) > self._max_message_bytes:
            raise PayloadTooLargeError("The normalized batch is too large; send smaller batches.")
        try:
            if await self.pending() >= self._max_pending:
                raise IngestUnavailableError("The ingest queue is full.", code="ingest_queue_full")
            await asyncio.to_thread(
                self._celery.send_task, WRITE_TASK, args=[payload], queue=self._queue
            )
        except IngestUnavailableError:
            raise
        except (RedisError, OSError, ConnectionError) as exc:
            raise IngestUnavailableError("The ingest queue is unreachable.") from exc
        except Exception as exc:  # kombu raises its own OperationalError family
            if type(exc).__module__.startswith(("kombu", "celery", "amqp")):
                raise IngestUnavailableError("The ingest queue is unreachable.") from exc
            raise
