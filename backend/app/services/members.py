"""Membership management inside one organization.

Rules (see app.core.tenancy.can_assign_role):
- owners manage everyone; admins manage only engineer/viewer memberships;
- anyone may remove themselves (leave);
- an org always keeps at least one owner (409 last_owner).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.rate_limit import RateLimiter
from app.core.tenancy import TenantContext, can_assign_role
from app.models.organization import OrganizationMember, Role
from app.models.user import User
from app.repositories.members import MemberRepository
from app.repositories.users import UserRepository
from app.services.audit import AuditService, RequestMeta
from app.services.auth import normalize_email


@dataclass(frozen=True, slots=True)
class MemberView:
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    created_at: datetime


def _view(member: OrganizationMember, user: User) -> MemberView:
    return MemberView(
        user_id=member.user_id,
        email=user.email,
        display_name=user.display_name,
        role=Role(member.role),
        created_at=member.created_at,
    )


class MemberService:
    def __init__(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        *,
        limiter: RateLimiter,
        settings: Settings,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.members = MemberRepository(session, ctx)
        self.users = UserRepository(session)
        self.audit = AuditService(session)
        self.limiter = limiter
        self.settings = settings

    async def list(self, *, limit: int, cursor: str | None) -> tuple[list[MemberView], str | None]:
        members, next_cursor = await self.members.list_page(limit=limit, cursor=cursor)
        users = await self.members.users_by_id([m.user_id for m in members])
        return [_view(m, users[m.user_id]) for m in members], next_cursor

    async def add(self, *, email: str, role: Role, meta: RequestMeta) -> MemberView:
        if not can_assign_role(self.ctx.role, None, role):
            raise ForbiddenError("You cannot grant this role.")
        # Looking users up by email reveals whether an account exists; throttle it.
        await self.limiter.hit(
            "members:add",
            str(self.ctx.user_id),
            limit=self.settings.auth_rate_limit_attempts,
            window_seconds=self.settings.auth_rate_limit_window_seconds,
        )
        user = await self.users.get_by_email(normalize_email(email))
        if user is None:
            raise NotFoundError("No user with this email.", code="user_not_found")
        if await self.members.get(user.id) is not None:
            raise ConflictError("User is already a member.", code="already_member")
        try:
            member = await self.members.add(OrganizationMember(user_id=user.id, role=role.value))
            self._audit("org.member.added", meta, user.id, {"role": role.value})
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("User is already a member.", code="already_member") from exc
        await self.session.refresh(member)
        return _view(member, user)

    async def change_role(self, user_id: uuid.UUID, *, role: Role, meta: RequestMeta) -> MemberView:
        member = await self._get_member(user_id)
        current = Role(member.role)
        if not can_assign_role(self.ctx.role, current, role):
            raise ForbiddenError("You cannot change this member's role.")
        if current is Role.OWNER and role is not Role.OWNER:
            await self._ensure_not_last_owner(user_id)
        if current is not role:
            member.role = role.value
            self._audit(
                "org.member.role_changed",
                meta,
                user_id,
                {"from": current.value, "to": role.value},
            )
            await self.session.commit()
            await self.session.refresh(member)
        user = await self.users.get(user_id)
        if user is None:  # FK makes this unreachable unless deleted concurrently
            raise NotFoundError("Member not found.", code="member_not_found")
        return _view(member, user)

    async def remove(self, user_id: uuid.UUID, *, meta: RequestMeta) -> None:
        member = await self._get_member(user_id)
        current = Role(member.role)
        leaving = user_id == self.ctx.user_id
        if not leaving and not can_assign_role(self.ctx.role, current, None):
            raise ForbiddenError("You cannot remove this member.")
        if current is Role.OWNER:
            await self._ensure_not_last_owner(user_id)
        await self.members.delete(member)
        self._audit("org.member.left" if leaving else "org.member.removed", meta, user_id, {})
        await self.session.commit()

    # ---- helpers -------------------------------------------------------------------------

    async def _get_member(self, user_id: uuid.UUID) -> OrganizationMember:
        member = await self.members.get(user_id)
        if member is None:
            raise NotFoundError("Member not found.", code="member_not_found")
        return member

    async def _ensure_not_last_owner(self, user_id: uuid.UUID) -> None:
        owners = await self.members.lock_owner_ids()
        if owners == [user_id]:
            raise ConflictError("An organization must keep at least one owner.", code="last_owner")

    def _audit(
        self, action: str, meta: RequestMeta, target: uuid.UUID, metadata: dict[str, object]
    ) -> None:
        self.audit.record(
            action,
            meta,
            org_id=self.ctx.org_id,
            actor_user_id=self.ctx.user_id,
            target_type="user",
            target_id=target,
            metadata=dict(metadata),
        )
