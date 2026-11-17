"""Organization lifecycle. T5 needs creation (registration); T6 adds the rest."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.core.slugs import slugify, with_random_suffix
from app.models.organization import Organization, OrganizationMember, Role
from app.repositories.organizations import MembershipRepository, OrganizationRepository
from app.services.audit import AuditService, RequestMeta


class OrgService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orgs = OrganizationRepository(session)
        self.members = MembershipRepository(session)
        self.audit = AuditService(session)

    async def create_org(
        self,
        *,
        name: str,
        slug: str | None,
        owner_id: uuid.UUID,
        meta: RequestMeta,
    ) -> Organization:
        """Create an org with `owner_id` as owner. Does not commit (caller owns the txn)."""
        if slug is not None:
            if await self.orgs.slug_exists(slug):
                raise ConflictError("Organization slug is already taken.", code="slug_taken")
        else:
            slug = slugify(name, fallback="org")
            if await self.orgs.slug_exists(slug):
                slug = with_random_suffix(slug)
        # A concurrent insert of the same slug still fails on uq_organizations_slug; the
        # caller maps that IntegrityError to 409 slug_taken.
        org = await self.orgs.add(Organization(name=name, slug=slug))
        await self.members.add(
            OrganizationMember(org_id=org.id, user_id=owner_id, role=Role.OWNER.value)
        )
        self.audit.record(
            "org.created",
            meta,
            org_id=org.id,
            actor_user_id=owner_id,
            target_type="organization",
            target_id=org.id,
            metadata={"slug": slug},
        )
        return org
