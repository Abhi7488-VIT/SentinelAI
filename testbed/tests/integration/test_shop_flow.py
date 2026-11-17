"""End-to-end flows through the published frontend (frontend -> gateway -> services -> DB).

Requires the running testbed (`make testbed-up`). Base URL: TESTBED_URL (default
http://127.0.0.1:8081).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE = os.environ.get("TESTBED_URL", "http://127.0.0.1:8081")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BASE, timeout=15, follow_redirects=False) as c:
        yield c


async def test_catalogue_pages(client: httpx.AsyncClient) -> None:
    home = await client.get("/")
    assert home.status_code == 200
    assert "Featured" in home.text
    audio = await client.get("/products", params={"category": "audio", "limit": 5})
    assert audio.status_code == 200
    skus = re.findall(r'href="/products/(SKU-[A-Z]{3}-\d{3})"', audio.text)
    assert len(skus) == 5
    assert all(s.startswith("SKU-AUD-") for s in skus)
    detail = await client.get(f"/products/{skus[0]}")
    assert detail.status_code == 200
    assert "Buy now" in detail.text
    missing = await client.get("/products/SKU-NOPE-999")
    assert missing.status_code == 404


async def test_login_account_and_checkout(client: httpx.AsyncClient) -> None:
    assert (await client.get("/account")).headers["location"] == "/login"

    bad = await client.post("/login", data={"email": "user1@shop.test", "password": "wrong"})
    assert bad.status_code == 401

    login = await client.post("/login", data={"email": "user7@shop.test", "password": "pw-7"})
    assert login.status_code == 303
    assert "shop_session" in login.cookies

    account = await client.get("/account")
    assert account.status_code == 200
    assert "Customer 7" in account.text
    history = re.findall(r"<li>#(\d+) ", account.text)
    assert 1 <= len(history) <= 20  # seeded history, newest first

    checkout = await client.post("/checkout", data={"sku": "SKU-KIT-004", "qty": "2"})
    # The issuer approves ~97% of orders deterministically per order id.
    assert checkout.status_code in (201, 402), checkout.text
    if checkout.status_code == 201:
        order_id = re.search(r"Order #(\d+) placed", checkout.text)
        assert order_id
        after = await client.get("/account")
        assert f"#{order_id.group(1)} " in after.text
