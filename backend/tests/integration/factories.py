"""Test helpers that create users/orgs/projects through the real API."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

import httpx

PASSWORD = "correct horse battery staple"
_seq = itertools.count()


@dataclass(frozen=True)
class Actor:
    user_id: str
    email: str
    token: str
    org_id: str  # the personal org created at registration (actor is its owner)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def register(client: httpx.AsyncClient, name: str, org_name: str | None = None) -> Actor:
    email = f"{name.lower()}-{next(_seq)}@example.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": name,
            "org_name": org_name or f"{name} Org",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return Actor(
        user_id=body["user"]["id"],
        email=email,
        token=body["access_token"],
        org_id=body["organization"]["id"],
    )


async def add_member(
    client: httpx.AsyncClient, owner: Actor, org_id: str, member: Actor, role: str
) -> None:
    resp = await client.post(
        f"/api/v1/orgs/{org_id}/members",
        json={"email": member.email, "role": role},
        headers=owner.headers,
    )
    assert resp.status_code == 201, resp.text


async def create_project(
    client: httpx.AsyncClient, actor: Actor, org_id: str, name: str = "Checkout", **extra: Any
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": name, **extra}, headers=actor.headers
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body
