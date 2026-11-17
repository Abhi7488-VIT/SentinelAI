"""Unit tests for the shop's business logic and shared plumbing (no running services)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shop.api_gateway.cache import ResponseCache
from shop.common.downstream import Downstream
from shop.common.errors import UpstreamError
from shop.common.releases import active_release
from shop.common.settings import ServiceSettings
from shop.payment_service.issuer import decide
from shop.user_service.security import hash_password, new_session_token, verify_password

# --- issuer -------------------------------------------------------------------------------


def test_issuer_is_deterministic_per_seed_and_order() -> None:
    assert decide(42, 1234) == decide(42, 1234)
    assert [decide(42, i).approved for i in range(500)] != [
        decide(7, i).approved for i in range(500)
    ]


def test_issuer_decline_rate_and_latency_bounds() -> None:
    decisions = [decide(42, i) for i in range(1, 20_001)]
    declined = sum(not d.approved for d in decisions)
    assert 0.025 <= declined / len(decisions) <= 0.035
    assert all(20 <= d.latency_ms <= 60 for d in decisions)
    assert all((d.decline_code is None) == d.approved for d in decisions)


# --- gateway cache ------------------------------------------------------------------------


def test_cache_hit_until_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("shop.api_gateway.cache.time.monotonic", lambda: now[0])
    cache = ResponseCache(ttl_s=30)
    cache.put("http://gw/api/products?limit=12", [{"sku": "A"}])
    assert cache.get("http://gw/api/products?limit=12") == [{"sku": "A"}]
    now[0] = 131.0
    assert cache.get("http://gw/api/products?limit=12") is None


def test_cache_key_is_the_full_url() -> None:
    cache = ResponseCache()
    cache.put("http://gw/api/products?limit=12", [1])
    assert cache.get("http://gw/api/products?limit=24") is None
    cache.put("http://gw/api/products?limit=24", [2])
    assert len(cache) == 2


# --- passwords ----------------------------------------------------------------------------


def test_password_hash_round_trip() -> None:
    encoded = hash_password("pw-17")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("pw-17", encoded)
    assert not verify_password("pw-18", encoded)
    assert not verify_password("pw-17", "garbage")
    assert new_session_token() != new_session_token()


# --- releases -----------------------------------------------------------------------------


def test_release_overrides_version_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "releases.json"
    manifest.write_text(
        json.dumps(
            {
                "services": {
                    "user-service": {"version": "2.4.0", "config": {"DATABASE_NAME": "users_v2"}}
                }
            }
        )
    )
    monkeypatch.setenv("RELEASES_FILE", str(manifest))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@postgres:5432/users")
    monkeypatch.setenv("SERVICE_VERSION", "2.3.0")
    settings = ServiceSettings.for_service("user-service")
    assert settings.service_version == "2.4.0"
    assert settings.database_dsn().endswith("@postgres:5432/users_v2")
    other = ServiceSettings.for_service("order-service")
    assert other.service_version == "2.3.0"  # no entry -> env version


def test_missing_manifest_means_no_release(tmp_path: Path) -> None:
    assert active_release(tmp_path / "absent.json", "user-service") is None


# --- downstream error mapping -------------------------------------------------------------


def _downstream(handler: httpx.MockTransport) -> Downstream:
    ds = Downstream("payment-service", "http://payment", timeout_s=1.0)
    ds.client = httpx.AsyncClient(base_url="http://payment", transport=handler)
    return ds


@pytest.mark.parametrize(
    ("behaviour", "status", "code"),
    [
        ("refused", 503, "dependency_unavailable"),
        ("timeout", 504, "dependency_timeout"),
        ("500", 502, "dependency_error"),
        ("402", 402, "payment_declined"),
    ],
)
async def test_downstream_failure_mapping(behaviour: str, status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if behaviour == "refused":
            raise httpx.ConnectError("refused", request=request)
        if behaviour == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if behaviour == "500":
            return httpx.Response(500, json={"error": {"code": "internal_error"}})
        return httpx.Response(
            402, json={"error": {"code": "payment_declined", "message": "Payment declined."}}
        )

    ds = _downstream(httpx.MockTransport(handler))
    with pytest.raises(UpstreamError) as exc:
        await ds.get_json("/x")
    assert exc.value.status_code == status
    assert exc.value.code == code


# --- frontend templates -------------------------------------------------------------------


def test_templates_render() -> None:
    from shop.frontend.main import templates

    env = templates.env
    html = env.get_template("products.html").render(
        title="Audio", products=[{"sku": "SKU-AUD-001", "name": "Speaker", "price_cents": 1299}]
    )
    assert "Speaker" in html
    assert "12.99" in html
    assert "No orders yet." in env.get_template("account.html").render(
        user={"name": "Customer 1"}, orders=[]
    )
