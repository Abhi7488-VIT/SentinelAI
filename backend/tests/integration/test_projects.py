"""Project CRUD, validation, slugs and pagination."""

from __future__ import annotations

import httpx
import pytest

from tests.integration.factories import create_project, register


async def test_project_crud(client: httpx.AsyncClient) -> None:
    alice = await register(client, "Alice")
    base = f"/api/v1/orgs/{alice.org_id}/projects"

    empty = await client.get(base, headers=alice.headers)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}

    created = await create_project(client, alice, alice.org_id, "Checkout API", description="d")
    assert created["slug"] == "checkout-api"
    assert created["org_id"] == alice.org_id
    assert created["created_by"] == alice.user_id

    got = await client.get(f"{base}/{created['id']}", headers=alice.headers)
    assert got.json()["name"] == "Checkout API"

    patched = await client.patch(
        f"{base}/{created['id']}",
        json={"name": "Checkout", "description": None},
        headers=alice.headers,
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Checkout"
    assert patched.json()["description"] is None
    assert patched.json()["slug"] == "checkout-api"  # slug is stable

    deleted = await client.delete(f"{base}/{created['id']}", headers=alice.headers)
    assert deleted.status_code == 204
    gone = await client.get(f"{base}/{created['id']}", headers=alice.headers)
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "project_not_found"


async def test_slug_conflicts(client: httpx.AsyncClient) -> None:
    alice = await register(client, "Alice")
    await create_project(client, alice, alice.org_id, "Checkout")
    # derived slug collides -> suffixed automatically
    second = await create_project(client, alice, alice.org_id, "Checkout")
    assert second["slug"].startswith("checkout-")
    # explicit slug collides -> 409
    resp = await client.post(
        f"/api/v1/orgs/{alice.org_id}/projects",
        json={"name": "Other", "slug": "checkout"},
        headers=alice.headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "slug_taken"


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"name": "ok", "slug": "Bad Slug"},
        {"name": "ok", "description": "x" * 2001},
        {"name": "ok", "org_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_create_validation(client: httpx.AsyncClient, body: dict[str, object]) -> None:
    alice = await register(client, "Alice")
    resp = await client.post(
        f"/api/v1/orgs/{alice.org_id}/projects", json=body, headers=alice.headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_patch_rejects_null_name(client: httpx.AsyncClient) -> None:
    alice = await register(client, "Alice")
    project = await create_project(client, alice, alice.org_id)
    resp = await client.patch(
        f"/api/v1/orgs/{alice.org_id}/projects/{project['id']}",
        json={"name": None},
        headers=alice.headers,
    )
    assert resp.status_code == 422


async def test_keyset_pagination_is_complete_and_stable(client: httpx.AsyncClient) -> None:
    alice = await register(client, "Alice")
    names = [f"Project {i:02d}" for i in range(7)]
    for name in names:
        await create_project(client, alice, alice.org_id, name)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get(
            f"/api/v1/orgs/{alice.org_id}/projects", params=params, headers=alice.headers
        )
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(p["name"] for p in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == list(reversed(names))  # newest first, no gaps or duplicates


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "garbage!"}])
async def test_bad_pagination_params(
    client: httpx.AsyncClient, params: dict[str, str | int]
) -> None:
    alice = await register(client, "Alice")
    resp = await client.get(
        f"/api/v1/orgs/{alice.org_id}/projects", params=params, headers=alice.headers
    )
    assert resp.status_code == 422
