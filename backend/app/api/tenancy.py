"""The org-membership gate. Every /orgs/{org_id}/... route depends on one of these.

- Not a member (or org doesn't exist)  -> 404 org_not_found  (no existence leak)
- Member with insufficient role         -> 403 forbidden
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Path

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import ForbiddenError, NotFoundError
from app.core.tenancy import TenantContext
from app.models.organization import Role
from app.repositories.organizations import MembershipRepository


def require_org_role(min_role: Role) -> Callable[..., Awaitable[TenantContext]]:
    async def dependency(
        user: CurrentUser,
        session: SessionDep,
        org_id: Annotated[uuid.UUID, Path()],
    ) -> TenantContext:
        member = await MembershipRepository(session).get(org_id, user.id)
        if member is None:
            raise NotFoundError("Organization not found.", code="org_not_found")
        role = Role(member.role)
        if not role.at_least(min_role):
            raise ForbiddenError()
        return TenantContext(org_id=org_id, user_id=user.id, role=role)

    dependency.__name__ = f"require_{min_role.value}"
    return dependency


ViewerCtx = Annotated[TenantContext, Depends(require_org_role(Role.VIEWER))]
EngineerCtx = Annotated[TenantContext, Depends(require_org_role(Role.ENGINEER))]
AdminCtx = Annotated[TenantContext, Depends(require_org_role(Role.ADMIN))]
