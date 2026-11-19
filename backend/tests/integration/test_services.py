"""Service registry: CRUD, validation, declared dependencies, archive, ingest API keys, roles."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit_log import AuditLog
from app.models.service import ServiceApiKey
from tests.integration.factories import (
    Actor,
    add_member,
    create_api_key,
    create_project,
    create_service,
    register,
)


def _base(actor: Actor) -> str:
    return f"/api/v1/orgs/{actor.org_id}/services"


@pytest.fixture
async def owner_project(client: httpx.AsyncClient) -> tuple[Actor, dict[str, Any]]:
    alice = await register(client, "Alice")
    project = await create_project(client, alice, alice.org_id, "Shop")
    return alice, project


# ---- CRUD ------------------------------------------------------------------------------------


async def test_service_crud(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    created = await create_service(
        client,
        alice,
        alice.org_id,
        project["id"],
        description="Orders API",
        repo_url="https://github.com/example/shop",
        deployment={"platform": "docker-compose", "current_version": "1.4.0"},
        telemetry_config={"memory_limit_bytes": 167772160},
    )
    assert created["name"] == "order-service"
    assert created["org_id"] == alice.org_id
    assert created["health_status"] == "unknown"
    assert created["last_seen_at"] is None
    assert created["dependencies"] == []
    assert created["deployment"]["platform"] == "docker-compose"
    assert created["telemetry_config"] == {"memory_limit_bytes": 167772160}

    url = f"{_base(alice)}/{created['id']}"
    got = await client.get(url, headers=alice.headers)
    assert got.status_code == 200
    assert got.json()["repo_url"] == "https://github.com/example/shop"

    patched = await client.patch(
        url,
        json={"description": None, "deployment": {"current_version": "1.5.0"}},
        headers=alice.headers,
    )
    assert patched.status_code == 200
    assert patched.json()["description"] is None
    # PATCH replaces the whole deployment object (no deep merge); unset fields are null.
    assert patched.json()["deployment"] == {
        "platform": None,
        "runtime": None,
        "region": None,
        "current_version": "1.5.0",
    }

    listed = await client.get(_base(alice), headers=alice.headers)
    assert [s["id"] for s in listed.json()["items"]] == [created["id"]]


async def test_list_filters(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    other = await create_project(client, alice, alice.org_id, "Other")
    await create_service(client, alice, alice.org_id, project["id"], "a", "production")
    await create_service(client, alice, alice.org_id, project["id"], "b", "staging")
    await create_service(client, alice, alice.org_id, other["id"], "c", "production")

    async def names(**params: str) -> list[str]:
        resp = await client.get(_base(alice), params=params, headers=alice.headers)
        assert resp.status_code == 200
        return sorted(s["name"] for s in resp.json()["items"])

    assert await names() == ["a", "b", "c"]
    assert await names(project_id=project["id"]) == ["a", "b"]
    assert await names(environment="production") == ["a", "c"]
    assert await names(project_id=project["id"], environment="staging") == ["b"]


async def test_name_unique_per_project_and_environment(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    await create_service(client, alice, alice.org_id, project["id"], "api", "production")
    # same name in another environment is fine
    await create_service(client, alice, alice.org_id, project["id"], "api", "staging")
    dup = await client.post(
        _base(alice),
        json={"project_id": project["id"], "name": "api", "environment": "production"},
        headers=alice.headers,
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "service_name_taken"

    # renaming into a taken name is also 409
    other = await create_service(client, alice, alice.org_id, project["id"], "web", "production")
    rename = await client.patch(
        f"{_base(alice)}/{other['id']}", json={"name": "api"}, headers=alice.headers
    )
    assert rename.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"name": "has space"},
        {"name": ""},
        {"name": "x" * 129},
        {"environment": "Prod"},
        {"environment": ""},
        {"repo_url": "ftp://example.com/repo"},
        {"repo_url": "not a url"},
        {"repo_url": "https://user:token@github.com/x/y"},
        {"deployment": {"unknown": "x"}},
        {"deployment": {"platform": "x" * 65}},
        {"telemetry_config": {"memory_limit_bytes": 10}},
        {"telemetry_config": {"api_key": "secret"}},
        {"health_status": "healthy"},  # never client-settable
        {"org_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_create_validation(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]], body: dict[str, Any]
) -> None:
    alice, project = owner_project
    payload = {"project_id": project["id"], "name": "ok", "environment": "production", **body}
    resp = await client.post(_base(alice), json=payload, headers=alice.headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("field", ["name", "environment", "deployment", "telemetry_config"])
async def test_patch_rejects_null_required(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]], field: str
) -> None:
    alice, project = owner_project
    svc = await create_service(client, alice, alice.org_id, project["id"])
    resp = await client.patch(
        f"{_base(alice)}/{svc['id']}", json={field: None}, headers=alice.headers
    )
    assert resp.status_code == 422


async def test_patch_cannot_move_project(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    other = await create_project(client, alice, alice.org_id, "Other")
    svc = await create_service(client, alice, alice.org_id, project["id"])
    resp = await client.patch(
        f"{_base(alice)}/{svc['id']}", json={"project_id": other["id"]}, headers=alice.headers
    )
    assert resp.status_code == 422


async def test_create_in_unknown_project_is_404(client: httpx.AsyncClient) -> None:
    alice = await register(client, "Alice")
    resp = await client.post(
        _base(alice),
        json={
            "project_id": "00000000-0000-0000-0000-000000000001",
            "name": "x",
            "environment": "production",
        },
        headers=alice.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "project_not_found"


# ---- declared dependencies ---------------------------------------------------------------------


async def test_set_dependencies_replaces_the_set(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    order = await create_service(client, alice, alice.org_id, project["id"], "order-service")
    payment = await create_service(client, alice, alice.org_id, project["id"], "payment-service")
    url = f"{_base(alice)}/{order['id']}/dependencies"

    resp = await client.put(
        url,
        json={
            "dependencies": [
                {"target_service_id": payment["id"], "description": "authorize payments"},
                {"target_external": "postgresql:orders"},
            ]
        },
        headers=alice.headers,
    )
    assert resp.status_code == 200, resp.text
    deps = resp.json()["dependencies"]
    # Order: service targets first, then externals by name.
    assert [(d["target_service_id"], d["target_external"]) for d in deps] == [
        (payment["id"], None),
        (None, "postgresql:orders"),
    ]

    replaced = await client.put(
        url, json={"dependencies": [{"target_external": "redis:sessions"}]}, headers=alice.headers
    )
    assert [d["target_external"] for d in replaced.json()["dependencies"]] == ["redis:sessions"]

    cleared = await client.put(url, json={"dependencies": []}, headers=alice.headers)
    assert cleared.json()["dependencies"] == []

    # the list endpoint returns dependencies too
    await client.put(
        url, json={"dependencies": [{"target_service_id": payment["id"]}]}, headers=alice.headers
    )
    listed = await client.get(_base(alice), headers=alice.headers)
    by_name = {s["name"]: s for s in listed.json()["items"]}
    assert by_name["order-service"]["dependencies"][0]["target_service_id"] == payment["id"]
    assert by_name["payment-service"]["dependencies"] == []


@pytest.mark.parametrize(
    ("make_deps", "status", "code"),
    [
        (lambda s, o, x: [{"target_service_id": s}], 422, "self_dependency"),
        (
            lambda s, o, x: [{"target_service_id": o}, {"target_service_id": o}],
            422,
            "duplicate_dependency",
        ),
        (
            lambda s, o, x: [{"target_external": "pg:a"}, {"target_external": "pg:a"}],
            422,
            "duplicate_dependency",
        ),
        (lambda s, o, x: [{"target_service_id": x}], 422, "invalid_dependency_target"),
        (
            lambda s, o, x: [{"target_service_id": "00000000-0000-0000-0000-000000000009"}],
            404,
            "service_not_found",
        ),
        (lambda s, o, x: [{}], 422, "validation_error"),
        (
            lambda s, o, x: [{"target_service_id": o, "target_external": "pg:a"}],
            422,
            "validation_error",
        ),
        (lambda s, o, x: [{"target_external": "has space"}], 422, "validation_error"),
    ],
)
async def test_invalid_dependencies(
    client: httpx.AsyncClient,
    owner_project: tuple[Actor, dict[str, Any]],
    make_deps: Any,
    status: int,
    code: str,
) -> None:
    alice, project = owner_project
    other_project = await create_project(client, alice, alice.org_id, "Other")
    svc = await create_service(client, alice, alice.org_id, project["id"], "a")
    peer = await create_service(client, alice, alice.org_id, project["id"], "b")
    cross = await create_service(client, alice, alice.org_id, other_project["id"], "c")
    resp = await client.put(
        f"{_base(alice)}/{svc['id']}/dependencies",
        json={"dependencies": make_deps(svc["id"], peer["id"], cross["id"])},
        headers=alice.headers,
    )
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code


async def test_dependency_on_archived_service_rejected(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    svc = await create_service(client, alice, alice.org_id, project["id"], "a")
    gone = await create_service(client, alice, alice.org_id, project["id"], "b")
    await client.delete(f"{_base(alice)}/{gone['id']}", headers=alice.headers)
    resp = await client.put(
        f"{_base(alice)}/{svc['id']}/dependencies",
        json={"dependencies": [{"target_service_id": gone["id"]}]},
        headers=alice.headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_dependency_target"


# ---- archive -----------------------------------------------------------------------------------


async def test_archive_service_revokes_keys_and_frees_name(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    svc = await create_service(client, alice, alice.org_id, project["id"], "api")
    key = await create_api_key(client, alice, alice.org_id, svc["id"])
    url = f"{_base(alice)}/{svc['id']}"

    assert (await client.delete(url, headers=alice.headers)).status_code == 204
    assert (await client.delete(url, headers=alice.headers)).status_code == 204  # idempotent

    got = await client.get(url, headers=alice.headers)
    assert got.status_code == 200
    assert got.json()["archived_at"] is not None

    keys = await client.get(f"{url}/api-keys", headers=alice.headers)
    [k] = keys.json()["items"]
    assert k["id"] == key["id"]
    assert k["revoked_at"] is not None

    # archived services are read-only and hidden from the default list
    patch = await client.patch(url, json={"description": "x"}, headers=alice.headers)
    assert patch.status_code == 409
    assert patch.json()["error"]["code"] == "service_archived"
    new_key = await client.post(f"{url}/api-keys", json={"name": "k"}, headers=alice.headers)
    assert new_key.status_code == 409
    assert (await client.get(_base(alice), headers=alice.headers)).json()["items"] == []
    with_archived = await client.get(
        _base(alice), params={"include_archived": "true"}, headers=alice.headers
    )
    assert [s["id"] for s in with_archived.json()["items"]] == [svc["id"]]

    # the name is free again
    again = await create_service(client, alice, alice.org_id, project["id"], "api")
    assert again["id"] != svc["id"]


async def test_archive_project_archives_services_and_revokes_keys(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    a = await create_service(client, alice, alice.org_id, project["id"], "a")
    b = await create_service(client, alice, alice.org_id, project["id"], "b")
    await create_api_key(client, alice, alice.org_id, a["id"])
    await create_api_key(client, alice, alice.org_id, b["id"])
    purl = f"/api/v1/orgs/{alice.org_id}/projects/{project['id']}"

    assert (await client.delete(purl, headers=alice.headers)).status_code == 204
    assert (await client.get(purl, headers=alice.headers)).json()["archived_at"] is not None

    for svc in (a, b):
        got = (await client.get(f"{_base(alice)}/{svc['id']}", headers=alice.headers)).json()
        assert got["archived_at"] is not None
        keys = await client.get(f"{_base(alice)}/{svc['id']}/api-keys", headers=alice.headers)
        assert all(k["revoked_at"] is not None for k in keys.json()["items"])

    new_service = await client.post(
        _base(alice),
        json={"project_id": project["id"], "name": "c", "environment": "production"},
        headers=alice.headers,
    )
    assert new_service.status_code == 409
    assert new_service.json()["error"]["code"] == "project_archived"


# ---- API keys ----------------------------------------------------------------------------------


async def test_api_key_shown_once_and_stored_hashed(
    client: httpx.AsyncClient,
    owner_project: tuple[Actor, dict[str, Any]],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    alice, project = owner_project
    svc = await create_service(client, alice, alice.org_id, project["id"])
    resp = await client.post(
        f"{_base(alice)}/{svc['id']}/api-keys", json={"name": "collector"}, headers=alice.headers
    )
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    created = resp.json()
    token = created["key"]
    prefix = token.split("_")[1]
    assert token.startswith("sai_")
    assert created["prefix"] == f"sai_{prefix}_…"
    assert created["service_id"] == svc["id"]
    assert created["revoked_at"] is None

    listed = await client.get(f"{_base(alice)}/{svc['id']}/api-keys", headers=alice.headers)
    [item] = listed.json()["items"]
    assert "key" not in item
    assert token not in listed.text

    async with sessionmaker() as s:
        row = (await s.scalars(select(ServiceApiKey).where(ServiceApiKey.prefix == prefix))).one()
        assert row.secret_hash == hashlib.sha256(token.encode()).digest()
        audit = (await s.scalars(select(AuditLog).where(AuditLog.target_id == created["id"]))).all()
    assert [a.action for a in audit] == ["service.api_key_created"]
    secret = token.split("_", 2)[2]
    assert all(secret not in str(a.metadata_) for a in audit)


async def test_revoke_key_is_idempotent(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    svc = await create_service(client, alice, alice.org_id, project["id"])
    key = await create_api_key(client, alice, alice.org_id, svc["id"])
    url = f"{_base(alice)}/{svc['id']}/api-keys/{key['id']}/revoke"
    first = await client.post(url, headers=alice.headers)
    assert first.status_code == 200
    assert first.json()["revoked_at"] is not None
    assert first.json()["revoked_by"] == alice.user_id
    second = await client.post(url, headers=alice.headers)
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


async def test_key_of_another_service_is_404(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    a = await create_service(client, alice, alice.org_id, project["id"], "a")
    b = await create_service(client, alice, alice.org_id, project["id"], "b")
    key_a = await create_api_key(client, alice, alice.org_id, a["id"])
    resp = await client.post(
        f"{_base(alice)}/{b['id']}/api-keys/{key_a['id']}/revoke", headers=alice.headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "api_key_not_found"


async def test_active_key_limit(
    client: httpx.AsyncClient, owner_project: tuple[Actor, dict[str, Any]]
) -> None:
    alice, project = owner_project
    svc = await create_service(client, alice, alice.org_id, project["id"])
    keys = [
        await create_api_key(client, alice, alice.org_id, svc["id"], f"k{i}") for i in range(10)
    ]
    url = f"{_base(alice)}/{svc['id']}/api-keys"
    over = await client.post(url, json={"name": "k10"}, headers=alice.headers)
    assert over.status_code == 409
    assert over.json()["error"]["code"] == "too_many_keys"
    await client.post(f"{url}/{keys[0]['id']}/revoke", headers=alice.headers)
    assert (await client.post(url, json={"name": "k10"}, headers=alice.headers)).status_code == 201


# ---- roles -------------------------------------------------------------------------------------


@pytest.fixture
async def team(client: httpx.AsyncClient) -> dict[str, Any]:
    owner = await register(client, "Owner", "Team Org")
    actors: dict[str, Any] = {"owner": owner}
    for role in ("admin", "engineer", "viewer"):
        actor = await register(client, role.capitalize())
        await add_member(client, owner, owner.org_id, actor, role)
        actors[role] = actor
    project = await create_project(client, owner, owner.org_id, "Shop")
    actors["service"] = await create_service(client, owner, owner.org_id, project["id"])
    actors["project"] = project
    return actors


@pytest.mark.parametrize(
    ("role", "read", "write", "admin"),
    [
        ("viewer", 200, 403, 403),
        ("engineer", 200, 200, 403),
        ("admin", 200, 200, 200),
        ("owner", 200, 200, 200),
    ],
)
async def test_role_matrix(
    client: httpx.AsyncClient, team: dict[str, Any], role: str, read: int, write: int, admin: int
) -> None:
    org = team["owner"].org_id
    svc = team["service"]
    h = team[role].headers
    url = f"/api/v1/orgs/{org}/services/{svc['id']}"

    assert (await client.get(f"/api/v1/orgs/{org}/services", headers=h)).status_code == read
    assert (await client.get(url, headers=h)).status_code == read
    assert (await client.patch(url, json={"description": role}, headers=h)).status_code == write
    deps = await client.put(f"{url}/dependencies", json={"dependencies": []}, headers=h)
    assert deps.status_code == write
    created = await client.post(
        f"/api/v1/orgs/{org}/services",
        json={"project_id": team["project"]["id"], "name": f"svc-{role}", "environment": "dev"},
        headers=h,
    )
    assert created.status_code == (201 if write == 200 else 403)

    assert (await client.get(f"{url}/api-keys", headers=h)).status_code == admin
    key = await client.post(f"{url}/api-keys", json={"name": role}, headers=h)
    assert key.status_code == (201 if admin == 200 else 403)
    # archive last (it makes the service read-only)
    archive = await client.delete(url, headers=h)
    assert archive.status_code == (204 if admin == 200 else 403)
