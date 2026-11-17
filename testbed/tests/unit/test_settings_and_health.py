"""Service settings and the liveness endpoint of every shop service."""

from __future__ import annotations

import importlib
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from shop.common.settings import SERVICE_NAMES, ServiceSettings

MODULES = {
    "frontend": "shop.frontend.main",
    "api-gateway": "shop.api_gateway.main",
    "user-service": "shop.user_service.main",
    "order-service": "shop.order_service.main",
    "payment-service": "shop.payment_service.main",
}


def test_every_service_has_an_entrypoint() -> None:
    assert set(MODULES) == set(SERVICE_NAMES)


def test_identity_comes_from_code_not_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "payment-service")
    monkeypatch.setenv("SERVICE_VERSION", "9.9.9")
    settings = ServiceSettings.for_service("order-service")
    assert settings.service_name == "order-service"
    assert settings.service_version == "9.9.9"


def test_unknown_service_rejected() -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(service_name="billing-service")


def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:hunter2@db/orders")
    settings = ServiceSettings.for_service("order-service")
    assert "hunter2" not in repr(settings)
    assert settings.database_url is not None
    assert "hunter2" in settings.database_url.get_secret_value()


@pytest.mark.parametrize(("service", "module"), sorted(MODULES.items()))
async def test_healthz(service: str, module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_VERSION", "1.2.3")
    create_app: Callable[[], FastAPI] = importlib.import_module(module).create_app
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": service, "version": "1.2.3"}
