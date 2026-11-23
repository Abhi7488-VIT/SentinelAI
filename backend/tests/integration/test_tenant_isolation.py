"""Cross-org isolation (CLAUDE.md invariant 4): another org's data is always 404."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.integration.factories import (
    Actor,
    create_api_key,
    create_project,
    create_service,
    register,
)


@pytest.fixture
async def two_orgs(client: httpx.AsyncClient) -> tuple[Actor, Actor, dict[str, Any]]:
    alice = await register(client, "Alice", "Org A")
    bob = await register(client, "Bob", "Org B")
    project_a = await create_project(client, alice, alice.org_id, "Secret A")
    service_a = await create_service(client, alice, alice.org_id, project_a["id"], "secret-svc")
    key_a = await create_api_key(client, alice, alice.org_id, service_a["id"])
    project_a["_service"] = service_a
    project_a["_key"] = key_a
    return alice, bob, project_a


def _assert_404(resp: httpx.Response) -> None:
    assert resp.status_code == 404, (resp.request.method, resp.request.url, resp.text)
    assert resp.json()["error"]["code"] in {
        "org_not_found",
        "project_not_found",
        "member_not_found",
        "service_not_found",
        "api_key_not_found",
        "not_found",
    }


async def test_outsider_cannot_see_or_touch_other_org(
    client: httpx.AsyncClient, two_orgs: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, bob, project = two_orgs
    org_a = f"/api/v1/orgs/{alice.org_id}"
    h = bob.headers
    _assert_404(await client.get(org_a, headers=h))
    _assert_404(await client.patch(org_a, json={"name": "pwned"}, headers=h))
    _assert_404(await client.get(f"{org_a}/members", headers=h))
    _assert_404(
        await client.post(f"{org_a}/members", json={"email": bob.email, "role": "owner"}, headers=h)
    )
    _assert_404(await client.get(f"{org_a}/audit-logs", headers=h))
    _assert_404(await client.get(f"{org_a}/projects", headers=h))
    _assert_404(await client.post(f"{org_a}/projects", json={"name": "x"}, headers=h))
    _assert_404(await client.get(f"{org_a}/projects/{project['id']}", headers=h))
    _assert_404(
        await client.patch(f"{org_a}/projects/{project['id']}", json={"name": "x"}, headers=h)
    )
    _assert_404(await client.delete(f"{org_a}/projects/{project['id']}", headers=h))


async def test_own_org_path_with_foreign_object_id_is_404(
    client: httpx.AsyncClient, two_orgs: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    """Bob is an owner of Org B; addressing Org A's project via Org B's path must fail."""
    alice, bob, project = two_orgs
    org_b = f"/api/v1/orgs/{bob.org_id}"
    h = bob.headers
    _assert_404(await client.get(f"{org_b}/projects/{project['id']}", headers=h))
    _assert_404(
        await client.patch(f"{org_b}/projects/{project['id']}", json={"name": "x"}, headers=h)
    )
    _assert_404(await client.delete(f"{org_b}/projects/{project['id']}", headers=h))
    member = await client.patch(
        f"{org_b}/members/{alice.user_id}", json={"role": "viewer"}, headers=h
    )
    _assert_404(member)
    _assert_404(await client.delete(f"{org_b}/members/{alice.user_id}", headers=h))


async def test_lists_and_data_stay_isolated(
    client: httpx.AsyncClient, two_orgs: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, bob, project = two_orgs
    await create_project(client, bob, bob.org_id, "Bob's")

    bob_projects = await client.get(f"/api/v1/orgs/{bob.org_id}/projects", headers=bob.headers)
    assert [p["name"] for p in bob_projects.json()["items"]] == ["Bob's"]
    bob_orgs = await client.get("/api/v1/orgs", headers=bob.headers)
    assert [o["id"] for o in bob_orgs.json()["items"]] == [bob.org_id]
    bob_audit = await client.get(f"/api/v1/orgs/{bob.org_id}/audit-logs", headers=bob.headers)
    assert all(e["actor_user_id"] != alice.user_id for e in bob_audit.json()["items"])

    # Alice's project is untouched by every attempt above.
    still = await client.get(
        f"/api/v1/orgs/{alice.org_id}/projects/{project['id']}", headers=alice.headers
    )
    assert still.status_code == 200
    assert still.json()["name"] == "Secret A"


async def test_every_org_scoped_route_hides_other_orgs(
    app: FastAPI,
    client: httpx.AsyncClient,
    two_orgs: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    """Guard: new /orgs/{org_id}/... routes automatically fall under this test."""
    alice, bob, project = two_orgs
    values = {
        "org_id": alice.org_id,
        "project_id": project["id"],
        "user_id": alice.user_id,
        "service_id": project["_service"]["id"],
        "key_id": project["_key"]["id"],
        "detection_id": "00000000-0000-4000-8000-000000000001",
        "incident_id": "00000000-0000-4000-8000-000000000002",
        "investigation_id": "00000000-0000-4000-8000-000000000003",
    }
    checked = 0
    # Enumerate endpoints from the public OpenAPI schema (covers every included router).
    for template, operations in app.openapi()["paths"].items():
        if "{org_id}" not in template:
            continue
        path = template.format(**values)
        for method in operations:
            # Query parameters too, so routes with a required `project_id` query are reached.
            resp = await client.request(
                method.upper(),
                path,
                json={},
                params={"project_id": project["id"]},
                headers=bob.headers,
            )
            _assert_404(resp)
            checked += 1
    assert checked >= 20, f"only {checked} org-scoped endpoints found"


async def test_unauthenticated_org_routes_are_401(
    client: httpx.AsyncClient, two_orgs: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _, _ = two_orgs
    resp = await client.get(f"/api/v1/orgs/{alice.org_id}/projects")
    assert resp.status_code == 401


async def test_foreign_service_and_key_ids_are_404(
    client: httpx.AsyncClient, two_orgs: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    """Bob owns Org B. Org A's service/key ids used under Org B's path must not resolve."""
    alice, bob, project = two_orgs
    svc, key = project["_service"], project["_key"]
    base = f"/api/v1/orgs/{bob.org_id}/services"
    h = bob.headers
    _assert_404(await client.get(f"{base}/{svc['id']}", headers=h))
    _assert_404(await client.patch(f"{base}/{svc['id']}", json={"description": "x"}, headers=h))
    _assert_404(await client.delete(f"{base}/{svc['id']}", headers=h))
    _assert_404(
        await client.put(f"{base}/{svc['id']}/dependencies", json={"dependencies": []}, headers=h)
    )
    _assert_404(await client.get(f"{base}/{svc['id']}/api-keys", headers=h))
    _assert_404(await client.post(f"{base}/{svc['id']}/api-keys", json={"name": "x"}, headers=h))
    _assert_404(await client.post(f"{base}/{svc['id']}/api-keys/{key['id']}/revoke", headers=h))

    # Bob's own service cannot reach Alice's key, depend on Alice's service, or be created in
    # Alice's project.
    bob_project = await create_project(client, bob, bob.org_id, "B")
    bob_svc = await create_service(client, bob, bob.org_id, bob_project["id"], "b")
    _assert_404(await client.post(f"{base}/{bob_svc['id']}/api-keys/{key['id']}/revoke", headers=h))
    _assert_404(
        await client.put(
            f"{base}/{bob_svc['id']}/dependencies",
            json={"dependencies": [{"target_service_id": svc["id"]}]},
            headers=h,
        )
    )
    _assert_404(
        await client.post(
            base,
            json={"project_id": project["id"], "name": "x", "environment": "production"},
            headers=h,
        )
    )
    listed = await client.get(base, params={"include_archived": "true"}, headers=h)
    assert [s["id"] for s in listed.json()["items"]] == [bob_svc["id"]]

    # Alice's service and key are untouched.
    a_base = f"/api/v1/orgs/{alice.org_id}/services/{svc['id']}"
    still = await client.get(a_base, headers=alice.headers)
    assert still.json()["archived_at"] is None
    keys = await client.get(f"{a_base}/api-keys", headers=alice.headers)
    assert keys.json()["items"][0]["revoked_at"] is None
