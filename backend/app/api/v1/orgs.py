"""Organization, membership and audit-log routes (thin)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, RateLimiterDep, RequestMetaDep, SessionDep, SettingsDep
from app.api.pagination import PageDep
from app.api.tenancy import AdminCtx, ViewerCtx
from app.models.organization import Organization, Role
from app.schemas.common import ErrorResponse, Page
from app.schemas.orgs import (
    AuditLogOut,
    MemberAdd,
    MemberOut,
    MemberUpdate,
    OrgCreate,
    OrgUpdate,
    OrgWithRoleOut,
)
from app.services.audit_query import AuditQueryService
from app.services.members import MemberService
from app.services.orgs import OrgService

router = APIRouter(prefix="/orgs", tags=["organizations"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 429)
}


def _with_role(org: Organization, role: Role) -> OrgWithRoleOut:
    return OrgWithRoleOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        created_at=org.created_at,
        updated_at=org.updated_at,
        role=role,
    )


# ---- organizations -----------------------------------------------------------------------


@router.get("", response_model=Page[OrgWithRoleOut], responses=_errors)
async def list_orgs(user: CurrentUser, session: SessionDep, page: PageDep) -> Page[OrgWithRoleOut]:
    rows, next_cursor = await OrgService(session).list_for_user(
        user.id, limit=page.limit, cursor=page.cursor
    )
    return Page[OrgWithRoleOut](
        items=[_with_role(org, role) for org, role in rows], next_cursor=next_cursor
    )


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=OrgWithRoleOut, responses=_errors
)
async def create_org(
    body: OrgCreate, user: CurrentUser, session: SessionDep, meta: RequestMetaDep
) -> OrgWithRoleOut:
    org = await OrgService(session).create(
        name=body.name, slug=body.slug, user_id=user.id, meta=meta
    )
    return _with_role(org, Role.OWNER)


@router.get("/{org_id}", response_model=OrgWithRoleOut, responses=_errors)
async def get_org(ctx: ViewerCtx, session: SessionDep) -> OrgWithRoleOut:
    return _with_role(await OrgService(session).get(ctx), ctx.role)


@router.patch("/{org_id}", response_model=OrgWithRoleOut, responses=_errors)
async def update_org(
    body: OrgUpdate, ctx: AdminCtx, session: SessionDep, meta: RequestMetaDep
) -> OrgWithRoleOut:
    org = await OrgService(session).update(ctx, name=body.name, meta=meta)
    return _with_role(org, ctx.role)


# ---- members -----------------------------------------------------------------------------


@router.get("/{org_id}/members", response_model=Page[MemberOut], responses=_errors)
async def list_members(
    ctx: ViewerCtx,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    page: PageDep,
) -> Page[MemberOut]:
    service = MemberService(session, ctx, limiter=limiter, settings=settings)
    views, next_cursor = await service.list(limit=page.limit, cursor=page.cursor)
    return Page[MemberOut](
        items=[MemberOut.model_validate(v) for v in views], next_cursor=next_cursor
    )


@router.post(
    "/{org_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=MemberOut,
    responses=_errors,
)
async def add_member(
    body: MemberAdd,
    ctx: AdminCtx,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    meta: RequestMetaDep,
) -> MemberOut:
    service = MemberService(session, ctx, limiter=limiter, settings=settings)
    return MemberOut.model_validate(await service.add(email=body.email, role=body.role, meta=meta))


@router.patch("/{org_id}/members/{user_id}", response_model=MemberOut, responses=_errors)
async def change_member_role(
    user_id: uuid.UUID,
    body: MemberUpdate,
    ctx: AdminCtx,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    meta: RequestMetaDep,
) -> MemberOut:
    service = MemberService(session, ctx, limiter=limiter, settings=settings)
    return MemberOut.model_validate(await service.change_role(user_id, role=body.role, meta=meta))


@router.delete(
    "/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors
)
async def remove_member(
    user_id: uuid.UUID,
    ctx: ViewerCtx,  # any member may leave; removing others is checked in the service
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    meta: RequestMetaDep,
) -> Response:
    service = MemberService(session, ctx, limiter=limiter, settings=settings)
    await service.remove(user_id, meta=meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- audit logs --------------------------------------------------------------------------


@router.get("/{org_id}/audit-logs", response_model=Page[AuditLogOut], responses=_errors)
async def list_audit_logs(ctx: AdminCtx, session: SessionDep, page: PageDep) -> Page[AuditLogOut]:
    logs, next_cursor = await AuditQueryService(session, ctx).list(
        limit=page.limit, cursor=page.cursor
    )
    return Page[AuditLogOut](
        items=[AuditLogOut.model_validate(log) for log in logs], next_cursor=next_cursor
    )
