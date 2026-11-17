"""Auth flows end-to-end against real Postgres + Redis."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.refresh_token import RefreshToken
from app.models.user import User
from tests.conftest import make_settings

COOKIE = "sentinel_refresh"
PASSWORD = "correct horse battery staple"


def _register_body(email: str = "alice@example.com", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": email,
        "password": PASSWORD,
        "display_name": "Alice",
        "org_name": "Acme Corp",
    }
    body.update(extra)
    return body


def _refresh_cookie(resp: httpx.Response) -> str:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE}="):
            value = header.split(";", 1)[0].split("=", 1)[1]
            if value and value != '""':
                return value
    raise AssertionError(f"no {COOKIE} cookie set: {resp.headers.get_list('set-cookie')}")


async def _post_with_cookie(
    client: httpx.AsyncClient, path: str, token: str | None, **kwargs: Any
) -> httpx.Response:
    client.cookies.clear()
    headers = dict(kwargs.pop("headers", {}))
    if token is not None:
        headers["Cookie"] = f"{COOKIE}={token}"
    return await client.post(path, headers=headers, **kwargs)


async def _register(client: httpx.AsyncClient, **kw: Any) -> httpx.Response:
    resp = await client.post("/api/v1/auth/register", json=_register_body(**kw))
    assert resp.status_code == 201, resp.text
    return resp


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _actions(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sm() as s:
        return list((await s.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


# --- register ---------------------------------------------------------------------------


async def test_register_creates_user_org_owner_and_session(client: httpx.AsyncClient) -> None:
    resp = await _register(client)
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["user"]["email"] == "alice@example.com"
    assert body["organization"]["name"] == "Acme Corp"
    assert body["organization"]["slug"] == "acme-corp"
    assert body["membership_role"] == "owner"
    assert "password" not in resp.text

    cookie_header = next(
        h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}=")
    )
    lowered = cookie_header.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/api/v1/auth" in lowered

    me = await client.get("/api/v1/auth/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["memberships"] == [
        {
            "org_id": body["organization"]["id"],
            "org_name": "Acme Corp",
            "org_slug": "acme-corp",
            "role": "owner",
        }
    ]


async def test_register_duplicate_email_case_insensitive(client: httpx.AsyncClient) -> None:
    await _register(client)
    resp = await client.post(
        "/api/v1/auth/register", json=_register_body(email="ALICE@Example.com")
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"


async def test_same_org_name_gets_unique_slug(client: httpx.AsyncClient) -> None:
    a = (await _register(client, email="a@example.com")).json()["organization"]["slug"]
    b = (await _register(client, email="b@example.com")).json()["organization"]["slug"]
    assert a == "acme-corp"
    assert b.startswith("acme-corp-")
    assert a != b


@pytest.mark.parametrize(
    ("override", "loc"),
    [
        ({"password": "short-pw"}, ["body", "password"]),
        ({"email": "not-an-email"}, ["body", "email"]),
        ({"display_name": "   "}, ["body", "display_name"]),
        ({"is_admin": True}, ["body", "is_admin"]),
    ],
)
async def test_register_validation(
    client: httpx.AsyncClient, override: dict[str, Any], loc: list[str]
) -> None:
    resp = await client.post("/api/v1/auth/register", json=_register_body(**override))
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["loc"] == loc
    assert "short-pw" not in resp.text


# --- login ------------------------------------------------------------------------------


async def test_login_success_and_uniform_failures(client: httpx.AsyncClient) -> None:
    await _register(client)
    ok = await client.post(
        "/api/v1/auth/login", json={"email": "Alice@Example.com", "password": PASSWORD}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == "alice@example.com"
    _refresh_cookie(ok)

    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "nope-nope-nope"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "bob@example.com", "password": "nope-nope-nope"}
    )
    for resp in (wrong, unknown):
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_credentials"
        assert "set-cookie" not in resp.headers
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_inactive_user_cannot_login_or_use_access_token(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = (await _register(client)).json()["access_token"]
    async with sessionmaker() as s:
        await s.execute(update(User).values(is_active=False))
        await s.commit()
    me = await client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "invalid_credentials"


# --- refresh / logout -------------------------------------------------------------------


async def test_refresh_rotates_and_reuse_revokes_family(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    first = _refresh_cookie(await _register(client))

    rotated = await _post_with_cookie(client, "/api/v1/auth/refresh", first)
    assert rotated.status_code == 200, rotated.text
    second = _refresh_cookie(rotated)
    assert second != first
    me = await client.get("/api/v1/auth/me", headers=_bearer(rotated.json()["access_token"]))
    assert me.status_code == 200

    # Replaying the rotated (old) token = theft signal: 401 and the whole family dies.
    replay = await _post_with_cookie(client, "/api/v1/auth/refresh", first)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_invalid"
    after = await _post_with_cookie(client, "/api/v1/auth/refresh", second)
    assert after.status_code == 401

    async with sessionmaker() as s:
        rows = (await s.scalars(select(RefreshToken))).all()
        assert {r.revoked_reason for r in rows} == {"rotated", "reuse_detected"}
        assert all(r.revoked_at is not None for r in rows)
    assert "auth.refresh.reuse_detected" in await _actions(sessionmaker)


async def test_refresh_without_cookie_or_with_garbage(client: httpx.AsyncClient) -> None:
    for token in (None, "garbage-token"):
        resp = await _post_with_cookie(client, "/api/v1/auth/refresh", token)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "refresh_token_invalid"


async def test_refresh_rejects_foreign_origin(client: httpx.AsyncClient) -> None:
    token = _refresh_cookie(await _register(client))
    resp = await _post_with_cookie(
        client, "/api/v1/auth/refresh", token, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "origin_not_allowed"


async def test_logout_revokes_session_and_is_idempotent(client: httpx.AsyncClient) -> None:
    token = _refresh_cookie(await _register(client))
    out = await _post_with_cookie(client, "/api/v1/auth/logout", token)
    assert out.status_code == 204
    cleared = [h for h in out.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}=")]
    assert cleared
    assert "max-age=0" in cleared[0].lower()

    assert (await _post_with_cookie(client, "/api/v1/auth/refresh", token)).status_code == 401
    assert (await _post_with_cookie(client, "/api/v1/auth/logout", token)).status_code == 204
    assert (await _post_with_cookie(client, "/api/v1/auth/logout", None)).status_code == 204


async def test_expired_refresh_token_rejected(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = _refresh_cookie(await _register(client))
    async with sessionmaker() as s:
        await s.execute(update(RefreshToken).values(expires_at=text("now() - interval '1 second'")))
        await s.commit()
    resp = await _post_with_cookie(client, "/api/v1/auth/refresh", token)
    assert resp.status_code == 401


# --- access token -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic abc"}],
)
async def test_me_requires_valid_bearer(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "not_authenticated"


# --- storage hygiene + audit ------------------------------------------------------------


async def test_secrets_never_stored_or_audited(
    client: httpx.AsyncClient, engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    reg = await _register(client)
    raw_refresh = _refresh_cookie(reg)
    await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "wrong-password!"}
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    await _post_with_cookie(client, "/api/v1/auth/refresh", _refresh_cookie(login))
    await _post_with_cookie(client, "/api/v1/auth/logout", raw_refresh)

    assert await _actions(sessionmaker) == [
        "org.created",
        "auth.register",
        "auth.login.failed",
        "auth.login.succeeded",
        "auth.refresh.succeeded",
        "auth.logout",
    ]
    rows: list[str] = []
    async with engine.connect() as conn:
        for table in ("users", "refresh_tokens", "audit_logs"):
            result = await conn.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
            rows.extend(str(row) for row in result.all())
    dump = " ".join(rows)
    assert "argon2id" in dump  # sanity: the dump really contains the stored rows
    assert PASSWORD not in dump
    assert "wrong-password!" not in dump
    assert raw_refresh not in dump
    async with sessionmaker() as s:
        failed = (
            await s.scalars(select(AuditLog).where(AuditLog.action == "auth.login.failed"))
        ).one()
        assert failed.metadata_ == {"reason": "bad_password"}
        assert failed.ip == "127.0.0.1"
        assert failed.request_id


# --- rate limiting ----------------------------------------------------------------------


@pytest.fixture
async def strict_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(make_settings(auth_rate_limit_attempts=3))
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


async def test_login_rate_limited(strict_client: httpx.AsyncClient) -> None:
    body = {"email": "nobody@example.com", "password": "whatever-password"}
    statuses = [
        (await strict_client.post("/api/v1/auth/login", json=body)).status_code for _ in range(4)
    ]
    assert statuses == [401, 401, 401, 429]
    resp = await strict_client.post("/api/v1/auth/login", json=body)
    assert resp.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(resp.headers["Retry-After"]) <= 60


async def test_rate_limiter_fails_closed_without_redis(engine: AsyncEngine) -> None:
    application = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.post(
                "/api/v1/auth/login", json={"email": "a@example.com", "password": "x" * 12}
            )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "rate_limiter_unavailable"


async def test_unknown_user_id_in_valid_token_rejected(client: httpx.AsyncClient) -> None:
    from app.core.security.tokens import create_access_token

    token = create_access_token(uuid.uuid4(), make_settings()).token
    resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
    assert resp.status_code == 401
