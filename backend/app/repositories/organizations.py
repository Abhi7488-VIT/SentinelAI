"""Organizations and memberships.

These are the *source* of tenancy, so they cannot themselves require a TenantContext:
- `OrganizationRepository` creates orgs and loads an org by id (callers have already checked
  membership via `MembershipRepository.get`).
- `MembershipRepository.get` / `list_orgs_for_user` filter by the authenticated user's own id.
Member management inside an org goes through the tenant-scoped repository in T6.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, OrganizationMember


class OrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, org_id: uuid.UUID) -> Organization | None:
        return await self.session.get(Organization, org_id)

    async def slug_exists(self, slug: str) -> bool:
        stmt = select(exists().where(Organization.slug == slug))
        return bool(await self.session.scalar(stmt))

    async def add(self, org: Organization) -> Organization:
        self.session.add(org)
        await self.session.flush()
        return org


class MembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, org_id: uuid.UUID, user_id: uuid.UUID) -> OrganizationMember | None:
        return await self.session.get(OrganizationMember, (org_id, user_id))

    async def add(self, member: OrganizationMember) -> OrganizationMember:
        self.session.add(member)
        await self.session.flush()
        return member

    async def list_orgs_for_user(
        self, user_id: uuid.UUID
    ) -> list[tuple[Organization, OrganizationMember]]:
        stmt = (
            select(Organization, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
            .where(OrganizationMember.user_id == user_id)
            .order_by(Organization.created_at, Organization.id)
        )
        rows = await self.session.execute(stmt)
        return [(org, member) for org, member in rows.tuples()]
