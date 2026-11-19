"""Ingest key resolution: header parsing, hash verification, revocation and the TTL cache."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.security.api_keys import generate_key
from app.ingest.auth import InvalidIngestKeyError, KeyResolver
from app.repositories.ingest_keys import KeyRecord

KEY = generate_key()


class FakeSessionmaker:
    def __call__(self) -> FakeSessionmaker:
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class Store:
    def __init__(self) -> None:
        self.record = KeyRecord(
            key_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            service_name="order-service",
            secret_hash=KEY.secret_hash,
            revoked_at=None,
            service_archived_at=None,
        )
        self.lookups = 0

    async def lookup(self, _session: Any, prefix: str) -> KeyRecord | None:
        self.lookups += 1
        return self.record if prefix == KEY.prefix else None


def _resolver(store: Store, now: list[float], ttl: float = 10.0) -> KeyResolver:
    sm: Any = FakeSessionmaker()
    return KeyResolver(sm, ttl=ttl, clock=lambda: now[0], lookup=store.lookup)


async def test_resolves_and_caches() -> None:
    store, now = Store(), [100.0]
    resolver = _resolver(store, now)
    principal = await resolver.resolve(f"Bearer {KEY.token}")
    assert principal.service_name == "order-service"
    assert principal.org_id == store.record.org_id
    await resolver.resolve(f"bearer {KEY.token}")
    assert store.lookups == 1  # cached


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        KEY.token,  # no scheme
        f"Basic {KEY.token}",
        "Bearer sai_nothex000000_" + "A" * 43,
        f"Bearer sai_{KEY.prefix}_" + "A" * 43,  # right prefix, wrong secret
        "Bearer sai_0123456789ab_" + "A" * 43,  # unknown prefix
    ],
)
async def test_rejects_bad_credentials(header: str | None) -> None:
    resolver = _resolver(Store(), [0.0])
    with pytest.raises(InvalidIngestKeyError) as exc:
        await resolver.resolve(header)
    assert exc.value.status_code == 401
    assert exc.value.headers is not None
    assert "WWW-Authenticate" in exc.value.headers


async def test_revocation_takes_effect_within_ttl() -> None:
    store, now = Store(), [100.0]
    resolver = _resolver(store, now, ttl=10.0)
    await resolver.resolve(f"Bearer {KEY.token}")
    store.record = _revoked(store.record)
    now[0] = 109.0
    await resolver.resolve(f"Bearer {KEY.token}")  # still cached: documented ≤ 10 s window
    now[0] = 110.5
    with pytest.raises(InvalidIngestKeyError):
        await resolver.resolve(f"Bearer {KEY.token}")


async def test_archived_service_is_rejected() -> None:
    store = Store()
    store.record = _revoked(store.record, archived=True)
    with pytest.raises(InvalidIngestKeyError):
        await _resolver(store, [0.0]).resolve(f"Bearer {KEY.token}")


def _revoked(r: KeyRecord, *, archived: bool = False) -> KeyRecord:
    now = datetime.now(UTC)
    return KeyRecord(
        key_id=r.key_id,
        org_id=r.org_id,
        service_id=r.service_id,
        service_name=r.service_name,
        secret_hash=r.secret_hash,
        revoked_at=None if archived else now,
        service_archived_at=now if archived else None,
    )
