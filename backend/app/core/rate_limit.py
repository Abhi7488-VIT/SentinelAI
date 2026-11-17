"""Fixed-window rate limiter backed by Redis.

Fails CLOSED: if Redis is unreachable the request is refused with 503, because silently
disabling brute-force protection on auth endpoints is the less safe failure mode.
Identifiers that may be personal data (emails) are hashed before becoming Redis keys.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import DependencyUnavailableError, RateLimitedError
from app.core.logging import get_logger

logger = get_logger("app.rate_limit")

# INCR the window counter; set its TTL only when it is created. Atomic.
_INCR_WITH_TTL = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


@dataclass(frozen=True, slots=True)
class Window:
    bucket: int
    retry_after: int


def current_window(now: float, window_seconds: int) -> Window:
    bucket = int(now // window_seconds)
    elapsed = now - bucket * window_seconds
    return Window(bucket=bucket, retry_after=max(1, int(window_seconds - elapsed + 0.999)))


def hashed_identifier(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "rl",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._clock = clock

    async def hit(self, scope: str, identifier: str, *, limit: int, window_seconds: int) -> None:
        """Count one attempt; raise RateLimitedError when over `limit` in the window."""
        window = current_window(self._clock(), window_seconds)
        key = f"{self._prefix}:{scope}:{identifier}:{window.bucket}"
        try:
            count = int(await self._redis.eval(_INCR_WITH_TTL, 1, key, window_seconds + 1))
        except (RedisError, OSError) as exc:
            logger.error("rate_limit.redis_unavailable", scope=scope, error=type(exc).__name__)
            raise DependencyUnavailableError(
                "Rate limiter unavailable; try again shortly.", code="rate_limiter_unavailable"
            ) from exc
        if count > limit:
            logger.warning("rate_limit.exceeded", scope=scope, count=count, limit=limit)
            raise RateLimitedError(retry_after_seconds=window.retry_after)
