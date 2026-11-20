"""Resolve `Authorization: Bearer sai_…` to an IngestPrincipal, with a short TTL cache.

The cache holds lookups (including misses) for `ttl` seconds per process, so a revoked key or an
archived service stops authenticating within `ttl` seconds (INGEST_KEY_CACHE_SECONDS, default 10).
Verification is always a constant-time hash comparison, cached or not.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.core.security.api_keys import parse_key, verify_key
from app.ingest.principal import IngestPrincipal
from app.ingest.queue import IngestUnavailableError
from app.repositories.ingest_keys import KeyRecord, find_key_by_prefix

_MAX_CACHE_ENTRIES = 10_000


class InvalidIngestKeyError(AppError):
    status_code = 401
    code = "invalid_api_key"
    message = "Missing, invalid or revoked ingest API key."

    def __init__(self) -> None:
        super().__init__(headers={"WWW-Authenticate": 'Bearer realm="sentinelai-ingest"'})


Lookup = Callable[[AsyncSession, str], Awaitable[KeyRecord | None]]


class KeyResolver:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
        lookup: Lookup = find_key_by_prefix,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._ttl = ttl
        self._clock = clock
        self._lookup = lookup
        self._cache: dict[str, tuple[float, KeyRecord | None]] = {}

    async def resolve(self, authorization: str | None) -> IngestPrincipal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer":
            raise InvalidIngestKeyError()
        parsed = parse_key(token.strip())
        if parsed is None:
            raise InvalidIngestKeyError()
        record = await self._record(parsed.prefix)
        if (
            record is None
            or record.revoked_at is not None
            or record.service_archived_at is not None
            or not verify_key(parsed, record.secret_hash)
        ):
            raise InvalidIngestKeyError()
        return IngestPrincipal(
            key_id=record.key_id,
            org_id=record.org_id,
            service_id=record.service_id,
            service_name=record.service_name,
        )

    async def _record(self, prefix: str) -> KeyRecord | None:
        now = self._clock()
        cached = self._cache.get(prefix)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with self._sessionmaker() as session:
                record = await self._lookup(session, prefix)
        except (SQLAlchemyError, OSError) as exc:
            # The database is unreachable: a retryable 503, not a 500 (senders back off).
            raise IngestUnavailableError("Key lookup unavailable.") from exc
        if len(self._cache) >= _MAX_CACHE_ENTRIES:
            self._cache.clear()  # bounded memory; a full reset is rare and only costs lookups
        self._cache[prefix] = (now + self._ttl, record)
        return record
