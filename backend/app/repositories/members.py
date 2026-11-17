"""Tenant-scoped membership management (list/add/change/remove within the current org)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.models.organization import OrganizationMember, Role
from app.models.user import User
from app.repositories.base import TenantRepository


class MemberRepository(TenantRepository[OrganizationMember]):
    model = OrganizationMember
    id_column = "user_id"

    async def lock_owner_ids(self) -> list[uuid.UUID]:
        """Owner user ids of this org, row-locked so last-owner checks can't race."""
        stmt = self.scoped().where(OrganizationMember.role == Role.OWNER.value).with_for_update()
        return [m.user_id for m in (await self.session.scalars(stmt)).all()]

    async def users_by_id(self, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, User]:
        """Users for a page of members (bounded by the page size)."""
        if not user_ids:
            return {}
        rows = await self.session.scalars(select(User).where(User.id.in_(user_ids)))
        return {u.id: u for u in rows.all()}
