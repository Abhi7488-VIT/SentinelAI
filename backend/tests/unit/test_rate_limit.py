"""Fixed-window rate limiter logic (fake Redis; real-Redis behaviour is in integration tests)."""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.errors import DependencyUnavailableError, RateLimitedError
from app.core.rate_limit import RateLimiter, current_window, hashed_identifier


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.fail = fail

    async def eval(self, _script: str, _numkeys: int, key: str, _ttl: int) -> int:
        if self.fail:
            raise RedisConnectionError("down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


def _limiter(fake: FakeRedis, now: list[float]) -> RateLimiter:
    redis: Any = fake
    return RateLimiter(redis, clock=lambda: now[0])


def test_current_window() -> None:
    assert current_window(120.0, 60).bucket == 2
    assert current_window(120.0, 60).retry_after == 60
    assert current_window(179.5, 60).retry_after == 1


async def test_allows_up_to_limit_then_blocks_with_retry_after() -> None:
    limiter = _limiter(FakeRedis(), [1000.0])
    for _ in range(3):
        await limiter.hit("login", "ip1", limit=3, window_seconds=60)
    with pytest.raises(RateLimitedError) as exc:
        await limiter.hit("login", "ip1", limit=3, window_seconds=60)
    assert exc.value.headers == {"Retry-After": "20"}  # 1000 % 60 = 40s elapsed


async def test_keys_are_isolated_by_scope_and_identifier() -> None:
    limiter = _limiter(FakeRedis(), [1000.0])
    await limiter.hit("login", "a", limit=1, window_seconds=60)
    await limiter.hit("login", "b", limit=1, window_seconds=60)
    await limiter.hit("register", "a", limit=1, window_seconds=60)


async def test_new_window_resets() -> None:
    now = [1000.0]
    limiter = _limiter(FakeRedis(), now)
    await limiter.hit("login", "a", limit=1, window_seconds=60)
    now[0] = 1021.0  # crosses the 1020 bucket boundary
    await limiter.hit("login", "a", limit=1, window_seconds=60)


async def test_fails_closed_when_redis_down() -> None:
    limiter = _limiter(FakeRedis(fail=True), [0.0])
    with pytest.raises(DependencyUnavailableError) as exc:
        await limiter.hit("login", "a", limit=5, window_seconds=60)
    assert exc.value.code == "rate_limiter_unavailable"


def test_hashed_identifier_normalizes_and_hides_email() -> None:
    a = hashed_identifier(" Alice@Example.com ")
    assert a == hashed_identifier("alice@example.com")
    assert "alice" not in a
