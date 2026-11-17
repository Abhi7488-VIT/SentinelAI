"""Organization lifecycle."""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.slugs import slugify, with_random_suffix
from app.core.tenancy import TenantContext
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

    async def create(
        self, *, name: str, slug: str | None, user_id: uuid.UUID, meta: RequestMeta
    ) -> Organization:
        try:
            org = await self.create_org(name=name, slug=slug, owner_id=user_id, meta=meta)
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("Organization slug is already taken.", code="slug_taken") from exc
        return org

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, cursor: str | None
    ) -> tuple[list[tuple[Organization, Role]], str | None]:
        rows, next_cursor = await self.members.page_orgs_for_user(
            user_id, limit=limit, cursor=cursor
        )
        return [(org, Role(role)) for org, role in rows], next_cursor

    async def get(self, ctx: TenantContext) -> Organization:
        org = await self.orgs.get(ctx.org_id)
        if org is None:  # membership existed a moment ago; org deleted concurrently
            raise NotFoundError("Organization not found.", code="org_not_found")
        return org

    async def update(
        self, ctx: TenantContext, *, name: str | None, meta: RequestMeta
    ) -> Organization:
        org = await self.get(ctx)
        if name is not None and name != org.name:
            old = org.name
            org.name = name
            self.audit.record(
                "org.updated",
                meta,
                org_id=org.id,
                actor_user_id=ctx.user_id,
                target_type="organization",
                target_id=org.id,
                metadata={"name": {"from": old, "to": name}},
            )
            await self.session.commit()
            await self.session.refresh(org)
        return org
