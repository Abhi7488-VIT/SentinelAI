"""Project routes (thin). Tenant scoping comes from the *Ctx dependency + ProjectRepository."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import RequestMetaDep, SessionDep
from app.api.pagination import PageDep
from app.api.tenancy import AdminCtx, EngineerCtx, ViewerCtx
from app.schemas.common import ErrorResponse, Page
from app.schemas.projects import ProjectCreate, ProjectOut, ProjectUpdate
from app.services.projects import ProjectService

router = APIRouter(prefix="/orgs/{org_id}/projects", tags=["projects"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409)
}


@router.get("", response_model=Page[ProjectOut], responses=_errors)
async def list_projects(ctx: ViewerCtx, session: SessionDep, page: PageDep) -> Page[ProjectOut]:
    projects, next_cursor = await ProjectService(session, ctx).list(
        limit=page.limit, cursor=page.cursor
    )
    return Page[ProjectOut](
        items=[ProjectOut.model_validate(p) for p in projects], next_cursor=next_cursor
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectOut, responses=_errors)
async def create_project(
    body: ProjectCreate, ctx: EngineerCtx, session: SessionDep, meta: RequestMetaDep
) -> ProjectOut:
    project = await ProjectService(session, ctx).create(
        name=body.name, slug=body.slug, description=body.description, meta=meta
    )
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut, responses=_errors)
async def get_project(project_id: uuid.UUID, ctx: ViewerCtx, session: SessionDep) -> ProjectOut:
    return ProjectOut.model_validate(await ProjectService(session, ctx).get(project_id))


@router.patch("/{project_id}", response_model=ProjectOut, responses=_errors)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    ctx: EngineerCtx,
    session: SessionDep,
    meta: RequestMetaDep,
) -> ProjectOut:
    project = await ProjectService(session, ctx).update(
        project_id, changes=body.changes(), meta=meta
    )
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors)
async def delete_project(
    project_id: uuid.UUID, ctx: AdminCtx, session: SessionDep, meta: RequestMetaDep
) -> Response:
    await ProjectService(session, ctx).delete(project_id, meta=meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
