"""Role-based access inside one organization."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit_log import AuditLog
from tests.integration.factories import Actor, add_member, create_project, register


@pytest.fixture
async def team(client: httpx.AsyncClient) -> dict[str, Actor]:
    owner = await register(client, "Owner", "Team Org")
    actors = {"owner": owner}
    for role in ("admin", "engineer", "viewer"):
        actor = await register(client, role.capitalize())
        await add_member(client, owner, owner.org_id, actor, role)
        actors[role] = actor
    return actors


def _url(team: dict[str, Actor], suffix: str = "") -> str:
    return f"/api/v1/orgs/{team['owner'].org_id}{suffix}"


@pytest.mark.parametrize(
    ("role", "expected"),
    [("viewer", 403), ("engineer", 201), ("admin", 201), ("owner", 201)],
)
async def test_create_project_requires_engineer(
    client: httpx.AsyncClient, team: dict[str, Actor], role: str, expected: int
) -> None:
    resp = await client.post(
        _url(team, "/projects"), json={"name": f"P {role}"}, headers=team[role].headers
    )
    assert resp.status_code == expected
    if expected == 403:
        assert resp.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    ("role", "expected"),
    [("viewer", 403), ("engineer", 403), ("admin", 204), ("owner", 204)],
)
async def test_delete_project_requires_admin(
    client: httpx.AsyncClient, team: dict[str, Actor], role: str, expected: int
) -> None:
    project = await create_project(client, team["owner"], team["owner"].org_id)
    resp = await client.delete(_url(team, f"/projects/{project['id']}"), headers=team[role].headers)
    assert resp.status_code == expected


async def test_viewer_can_read(client: httpx.AsyncClient, team: dict[str, Actor]) -> None:
    project = await create_project(client, team["owner"], team["owner"].org_id)
    viewer = team["viewer"].headers
    assert (await client.get(_url(team, "/projects"), headers=viewer)).status_code == 200
    got = await client.get(_url(team, f"/projects/{project['id']}"), headers=viewer)
    assert got.status_code == 200
    assert (await client.get(_url(team), headers=viewer)).json()["role"] == "viewer"
    members = await client.get(_url(team, "/members"), headers=viewer)
    assert {m["role"] for m in members.json()["items"]} == {"owner", "admin", "engineer", "viewer"}
    assert (await client.patch(_url(team), json={"name": "x"}, headers=viewer)).status_code == 403


@pytest.mark.parametrize(("role", "expected"), [("engineer", 403), ("admin", 200)])
async def test_audit_logs_admin_only(
    client: httpx.AsyncClient, team: dict[str, Actor], role: str, expected: int
) -> None:
    resp = await client.get(_url(team, "/audit-logs"), headers=team[role].headers)
    assert resp.status_code == expected
    if expected == 200:
        actions = [e["action"] for e in resp.json()["items"]]
        assert actions.count("org.member.added") == 3
        assert "org.created" in actions


async def test_admin_cannot_grant_admin_or_owner(
    client: httpx.AsyncClient, team: dict[str, Actor]
) -> None:
    admin = team["admin"].headers
    newcomer = await register(client, "Newcomer")
    for role in ("admin", "owner"):
        resp = await client.post(
            _url(team, "/members"), json={"email": newcomer.email, "role": role}, headers=admin
        )
        assert resp.status_code == 403
    ok = await client.post(
        _url(team, "/members"), json={"email": newcomer.email, "role": "engineer"}, headers=admin
    )
    assert ok.status_code == 201
    # admin cannot promote to admin, nor touch the owner
    promote = await client.patch(
        _url(team, f"/members/{newcomer.user_id}"), json={"role": "admin"}, headers=admin
    )
    assert promote.status_code == 403
    demote_owner = await client.patch(
        _url(team, f"/members/{team['owner'].user_id}"), json={"role": "viewer"}, headers=admin
    )
    assert demote_owner.status_code == 403
    remove_owner = await client.delete(
        _url(team, f"/members/{team['owner'].user_id}"), headers=admin
    )
    assert remove_owner.status_code == 403


async def test_engineer_cannot_manage_members(
    client: httpx.AsyncClient, team: dict[str, Actor]
) -> None:
    resp = await client.patch(
        _url(team, f"/members/{team['viewer'].user_id}"),
        json={"role": "engineer"},
        headers=team["engineer"].headers,
    )
    assert resp.status_code == 403


async def test_last_owner_protection_and_owner_transfer(
    client: httpx.AsyncClient, team: dict[str, Actor]
) -> None:
    owner = team["owner"]
    demote = await client.patch(
        _url(team, f"/members/{owner.user_id}"), json={"role": "admin"}, headers=owner.headers
    )
    assert demote.status_code == 409
    assert demote.json()["error"]["code"] == "last_owner"
    leave = await client.delete(_url(team, f"/members/{owner.user_id}"), headers=owner.headers)
    assert leave.status_code == 409

    promote = await client.patch(
        _url(team, f"/members/{team['admin'].user_id}"),
        json={"role": "owner"},
        headers=owner.headers,
    )
    assert promote.status_code == 200
    leave_now = await client.delete(_url(team, f"/members/{owner.user_id}"), headers=owner.headers)
    assert leave_now.status_code == 204
    gone = await client.get(_url(team), headers=owner.headers)
    assert gone.status_code == 404  # no longer a member -> org is invisible


async def test_member_can_leave_and_errors(
    client: httpx.AsyncClient, team: dict[str, Actor]
) -> None:
    viewer = team["viewer"]
    left = await client.delete(_url(team, f"/members/{viewer.user_id}"), headers=viewer.headers)
    assert left.status_code == 204

    owner = team["owner"].headers
    unknown = await client.post(
        _url(team, "/members"),
        json={"email": "ghost@example.com", "role": "viewer"},
        headers=owner,
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "user_not_found"
    dup = await client.post(
        _url(team, "/members"),
        json={"email": team["engineer"].email, "role": "viewer"},
        headers=owner,
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "already_member"


async def test_member_changes_are_audited_with_actor(
    client: httpx.AsyncClient,
    team: dict[str, Actor],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await client.patch(
        _url(team, f"/members/{team['viewer'].user_id}"),
        json={"role": "engineer"},
        headers=team["admin"].headers,
    )
    async with sessionmaker() as s:
        entry = (
            await s.scalars(select(AuditLog).where(AuditLog.action == "org.member.role_changed"))
        ).one()
    assert str(entry.org_id) == team["owner"].org_id
    assert str(entry.actor_user_id) == team["admin"].user_id
    assert entry.target_id == team["viewer"].user_id
    assert entry.metadata_ == {"from": "viewer", "to": "engineer"}


async def test_create_and_list_orgs(client: httpx.AsyncClient) -> None:
    alice = await register(client, "Alice")
    created = await client.post(
        "/api/v1/orgs", json={"name": "Second Org", "slug": "second-org"}, headers=alice.headers
    )
    assert created.status_code == 201
    assert created.json()["role"] == "owner"
    dup = await client.post(
        "/api/v1/orgs", json={"name": "Dup", "slug": "second-org"}, headers=alice.headers
    )
    assert dup.status_code == 409
    listed = await client.get("/api/v1/orgs", headers=alice.headers)
    assert [o["name"] for o in listed.json()["items"]] == ["Second Org", "Alice Org"]
