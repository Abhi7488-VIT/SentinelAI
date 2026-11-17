"""Uniform error shape, request-id propagation and security headers."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.errors import ConflictError, NotFoundError, RateLimitedError
from app.main import create_app
from tests.conftest import make_settings


class _Body(BaseModel):
    email: str
    password: str
    age: int


def _build_app() -> FastAPI:
    app = create_app(make_settings())

    @app.get("/_test/not-found")
    async def raise_not_found() -> None:
        raise NotFoundError("Thing not found.", code="thing_not_found")

    @app.get("/_test/conflict")
    async def raise_conflict() -> None:
        raise ConflictError()

    @app.get("/_test/rate-limited")
    async def raise_rate_limited() -> None:
        raise RateLimitedError(retry_after_seconds=42)

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals must not leak")

    @app.post("/_test/validate")
    async def validate(body: _Body) -> dict[str, str]:
        return {"ok": body.email}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_error(resp: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert error["request_id"] == resp.headers["X-Request-ID"]
    return dict(error)


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_unknown_route_returns_uniform_404(client: httpx.AsyncClient) -> None:
    _assert_error(await client.get("/does-not-exist"), 404, "not_found")


async def test_method_not_allowed(client: httpx.AsyncClient) -> None:
    _assert_error(await client.delete("/health"), 405, "method_not_allowed")


async def test_app_error_custom_code(client: httpx.AsyncClient) -> None:
    error = _assert_error(await client.get("/_test/not-found"), 404, "thing_not_found")
    assert error["message"] == "Thing not found."
    assert "details" not in error


async def test_app_error_default_code(client: httpx.AsyncClient) -> None:
    _assert_error(await client.get("/_test/conflict"), 409, "conflict")


async def test_rate_limited_sets_retry_after(client: httpx.AsyncClient) -> None:
    resp = await client.get("/_test/rate-limited")
    _assert_error(resp, 429, "rate_limited")
    assert resp.headers["Retry-After"] == "42"


async def test_validation_error_has_details_and_does_not_echo_input(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/_test/validate", json={"email": "a@b.c", "password": "hunter2-SUPERSECRET", "age": "x"}
    )
    error = _assert_error(resp, 422, "validation_error")
    details = error["details"]
    assert isinstance(details, list)
    assert details[0]["loc"] == ["body", "age"]
    assert set(details[0]) == {"loc", "msg", "type"}
    assert "SUPERSECRET" not in resp.text


async def test_unhandled_exception_returns_500_without_internals(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/_test/boom")
    _assert_error(resp, 500, "internal_error")
    assert "secret internals" not in resp.text


async def test_request_id_is_echoed_when_valid(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "abc-123_DEF.4"})
    assert resp.headers["X-Request-ID"] == "abc-123_DEF.4"


@pytest.mark.parametrize("bad", ["has spaces", "x" * 129, "inject\nheader", ""])
async def test_invalid_request_id_is_replaced(client: httpx.AsyncClient, bad: str) -> None:
    try:
        resp = await client.get("/health", headers={"X-Request-ID": bad})
    except httpx.LocalProtocolError:
        pytest.skip("client refuses to send this header value")
    rid = resp.headers["X-Request-ID"]
    assert rid != bad
    assert len(rid) == 32
    int(rid, 16)


async def test_security_headers(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


async def test_metrics_endpoint_exposes_http_metrics(client: httpx.AsyncClient) -> None:
    await client.get("/health")
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert 'http_requests_total{method="GET",route="/health",status="200"}' in resp.text
