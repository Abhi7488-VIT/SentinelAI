"""Service registry routes (thin). Roles: viewer reads, engineer writes, admin archives and
manages ingest keys. Tenant scoping comes from the *Ctx dependency + scoped repositories."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status

from app.api.deps import RequestMetaDep, SessionDep
from app.api.pagination import PageDep
from app.api.tenancy import AdminCtx, EngineerCtx, ViewerCtx
from app.core.security.api_keys import display_prefix
from app.models.service import ServiceApiKey
from app.schemas.common import ErrorResponse, Page
from app.schemas.services import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    DependenciesPut,
    DependencyOut,
    Environment,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
)
from app.services.service_registry import ServiceRegistryService, ServiceView

router = APIRouter(prefix="/orgs/{org_id}/services", tags=["services"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)
}


def _service_out(view: ServiceView) -> ServiceOut:
    s = view.service
    return ServiceOut.model_validate(
        {
            **{c: getattr(s, c) for c in ServiceOut.model_fields if c != "dependencies"},
            "dependencies": [DependencyOut.model_validate(d) for d in view.dependencies],
        }
    )


def _key_out[T: ApiKeyOut](row: ServiceApiKey, model: type[T], **extra: Any) -> T:
    return model.model_validate(
        {
            **{c: getattr(row, c) for c in ApiKeyOut.model_fields if c != "prefix"},
            "prefix": display_prefix(row.prefix),
            **extra,
        }
    )


@router.get("", response_model=Page[ServiceOut], responses=_errors)
async def list_services(
    ctx: ViewerCtx,
    session: SessionDep,
    page: PageDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    environment: Annotated[Environment | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
) -> Page[ServiceOut]:
    views, next_cursor = await ServiceRegistryService(session, ctx).list_services(
        limit=page.limit,
        cursor=page.cursor,
        project_id=project_id,
        environment=environment,
        include_archived=include_archived,
    )
    return Page[ServiceOut](items=[_service_out(v) for v in views], next_cursor=next_cursor)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ServiceOut, responses=_errors)
async def create_service(
    body: ServiceCreate, ctx: EngineerCtx, session: SessionDep, meta: RequestMetaDep
) -> ServiceOut:
    view = await ServiceRegistryService(session, ctx).create(
        project_id=body.project_id,
        name=body.name,
        environment=body.environment,
        description=body.description,
        repo_url=str(body.repo_url) if body.repo_url is not None else None,
        deployment=body.deployment.model_dump(exclude_none=True),
        telemetry_config=body.telemetry_config.model_dump(exclude_none=True),
        meta=meta,
    )
    return _service_out(view)


@router.get("/{service_id}", response_model=ServiceOut, responses=_errors)
async def get_service(service_id: uuid.UUID, ctx: ViewerCtx, session: SessionDep) -> ServiceOut:
    return _service_out(await ServiceRegistryService(session, ctx).get_view(service_id))


@router.patch("/{service_id}", response_model=ServiceOut, responses=_errors)
async def update_service(
    service_id: uuid.UUID,
    body: ServiceUpdate,
    ctx: EngineerCtx,
    session: SessionDep,
    meta: RequestMetaDep,
) -> ServiceOut:
    view = await ServiceRegistryService(session, ctx).update(
        service_id, changes=body.changes(), meta=meta
    )
    return _service_out(view)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors)
async def archive_service(
    service_id: uuid.UUID, ctx: AdminCtx, session: SessionDep, meta: RequestMetaDep
) -> Response:
    """Archive (never hard-delete) and revoke all of the service's ingest keys."""
    await ServiceRegistryService(session, ctx).archive(service_id, meta=meta)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{service_id}/dependencies", response_model=ServiceOut, responses=_errors)
async def set_dependencies(
    service_id: uuid.UUID,
    body: DependenciesPut,
    ctx: EngineerCtx,
    session: SessionDep,
    meta: RequestMetaDep,
) -> ServiceOut:
    view = await ServiceRegistryService(session, ctx).set_dependencies(
        service_id, body.dependencies, meta=meta
    )
    return _service_out(view)


@router.get("/{service_id}/api-keys", response_model=Page[ApiKeyOut], responses=_errors)
async def list_api_keys(
    service_id: uuid.UUID, ctx: AdminCtx, session: SessionDep, page: PageDep
) -> Page[ApiKeyOut]:
    keys, next_cursor = await ServiceRegistryService(session, ctx).list_keys(
        service_id, limit=page.limit, cursor=page.cursor
    )
    return Page[ApiKeyOut](items=[_key_out(k, ApiKeyOut) for k in keys], next_cursor=next_cursor)


@router.post(
    "/{service_id}/api-keys",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreated,
    responses=_errors,
)
async def create_api_key(
    service_id: uuid.UUID,
    body: ApiKeyCreate,
    ctx: AdminCtx,
    session: SessionDep,
    meta: RequestMetaDep,
    response: Response,
) -> ApiKeyCreated:
    """The plaintext `key` is in this response only; store it now."""
    created = await ServiceRegistryService(session, ctx).create_key(
        service_id, name=body.name, meta=meta
    )
    response.headers["Cache-Control"] = "no-store"
    return _key_out(created.key, ApiKeyCreated, key=created.token)


@router.post("/{service_id}/api-keys/{key_id}/revoke", response_model=ApiKeyOut, responses=_errors)
async def revoke_api_key(
    service_id: uuid.UUID,
    key_id: uuid.UUID,
    ctx: AdminCtx,
    session: SessionDep,
    meta: RequestMetaDep,
) -> ApiKeyOut:
    key = await ServiceRegistryService(session, ctx).revoke_key(service_id, key_id, meta=meta)
    return _key_out(key, ApiKeyOut)
