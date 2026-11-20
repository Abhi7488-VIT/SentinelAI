"""Service registry: services, declared dependencies and ingest API keys.

Tenant isolation comes from the scoped repositories; this module never filters by org itself.
Every mutating use case commits once and writes its audit row in the same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidRequestError, NotFoundError
from app.core.security.api_keys import generate_key
from app.core.tenancy import TenantContext
from app.models.service import Service, ServiceApiKey, ServiceDependency
from app.repositories.service_graph import ServiceEdgeRepository
from app.repositories.services import (
    ServiceApiKeyRepository,
    ServiceDependencyRepository,
    ServiceRepository,
)
from app.schemas.services import DependencyIn
from app.services.audit import AuditService, RequestMeta
from app.services.projects import ProjectService

MAX_ACTIVE_KEYS_PER_SERVICE = 10


@dataclass(frozen=True, slots=True)
class ServiceView:
    service: Service
    dependencies: list[ServiceDependency]


@dataclass(frozen=True, slots=True)
class CreatedKey:
    key: ServiceApiKey
    token: str  # plaintext, returned once


class ServiceRegistryService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.session = session
        self.ctx = ctx
        self.services = ServiceRepository(session, ctx)
        self.deps = ServiceDependencyRepository(session, ctx)
        self.keys = ServiceApiKeyRepository(session, ctx)
        self.edges = ServiceEdgeRepository(session, ctx)
        self.audit = AuditService(session)

    # ---- services -------------------------------------------------------------------------

    async def list_services(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: uuid.UUID | None,
        environment: str | None,
        include_archived: bool,
    ) -> tuple[list[ServiceView], str | None]:
        services, next_cursor = await self.services.list_filtered(
            limit=limit,
            cursor=cursor,
            project_id=project_id,
            environment=environment,
            include_archived=include_archived,
        )
        deps = await self.deps.for_services([s.id for s in services])
        return [ServiceView(s, deps[s.id]) for s in services], next_cursor

    async def get(self, service_id: uuid.UUID) -> Service:
        service = await self.services.get(service_id)
        if service is None:
            raise NotFoundError("Service not found.", code="service_not_found")
        return service

    async def get_view(self, service_id: uuid.UUID) -> ServiceView:
        service = await self.get(service_id)
        return ServiceView(service, await self.deps.for_service(service.id))

    async def _get_active(self, service_id: uuid.UUID) -> Service:
        service = await self.get(service_id)
        if service.archived_at is not None:
            raise ConflictError("The service is archived.", code="service_archived")
        return service

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        environment: str,
        description: str | None,
        repo_url: str | None,
        deployment: dict[str, Any],
        telemetry_config: dict[str, Any],
        meta: RequestMeta,
    ) -> ServiceView:
        # 404 for an unknown/foreign project, 409 if archived.
        await ProjectService(self.session, self.ctx).get_active(project_id)
        if await self.services.active_name_exists(
            project_id=project_id, environment=environment, name=name, exclude_id=None
        ):
            raise _name_taken()
        try:
            service = await self.services.add(
                Service(
                    project_id=project_id,
                    name=name,
                    environment=environment,
                    description=description,
                    repo_url=repo_url,
                    deployment=deployment,
                    telemetry_config=telemetry_config,
                    created_by=self.ctx.user_id,
                )
            )
            self._audit(
                "service.created",
                meta,
                "service",
                service.id,
                {"name": name, "environment": environment, "project_id": str(project_id)},
            )
            await self.session.commit()
        except IntegrityError as exc:  # concurrent create with the same name
            await self.session.rollback()
            raise _name_taken() from exc
        await self.session.refresh(service)
        return ServiceView(service, [])

    async def update(
        self, service_id: uuid.UUID, *, changes: dict[str, Any], meta: RequestMeta
    ) -> ServiceView:
        service = await self._get_active(service_id)
        applied = {k: v for k, v in changes.items() if getattr(service, k) != v}
        if applied:
            renamed = "name" in applied or "environment" in applied
            if renamed and await self.services.active_name_exists(
                project_id=service.project_id,
                environment=applied.get("environment", service.environment),
                name=applied.get("name", service.name),
                exclude_id=service.id,
            ):
                raise _name_taken()
            for key, value in applied.items():
                setattr(service, key, value)
            self._audit("service.updated", meta, "service", service.id, {"fields": sorted(applied)})
            try:
                await self.session.commit()
            except IntegrityError as exc:
                await self.session.rollback()
                raise _name_taken() from exc
            await self.session.refresh(service)
        return ServiceView(service, await self.deps.for_service(service.id))

    async def archive(self, service_id: uuid.UUID, *, meta: RequestMeta) -> None:
        """Archive the service and revoke its keys. Idempotent."""
        service = await self.get(service_id)
        if service.archived_at is not None:
            return
        now = datetime.now(UTC)
        service.archived_at = now
        revoked = await self.keys.revoke_all_for_services([service.id], at=now, by=self.ctx.user_id)
        self._audit(
            "service.archived",
            meta,
            "service",
            service.id,
            {"name": service.name, "keys_revoked": len(revoked)},
        )
        await self.session.commit()

    # ---- declared dependencies ----------------------------------------------------------

    async def set_dependencies(
        self, service_id: uuid.UUID, deps: list[DependencyIn], *, meta: RequestMeta
    ) -> ServiceView:
        """Replace the declared dependency set. Targets must be active services in the same
        project (an unknown or foreign id is 404, like any other object id)."""
        service = await self._get_active(service_id)
        target_ids = [d.target_service_id for d in deps if d.target_service_id is not None]
        externals = [d.target_external for d in deps if d.target_external is not None]
        if len(set(target_ids)) != len(target_ids) or len(set(externals)) != len(externals):
            raise InvalidRequestError("Duplicate dependency target.", code="duplicate_dependency")
        if service.id in target_ids:
            raise InvalidRequestError("A service cannot depend on itself.", code="self_dependency")
        targets = {t.id: t for t in await self.services.get_many(target_ids)}
        for target_id in target_ids:
            target = targets.get(target_id)
            if target is None:
                raise NotFoundError(
                    "Dependency target service not found.", code="service_not_found"
                )
            if target.project_id != service.project_id or target.archived_at is not None:
                raise InvalidRequestError(
                    "Dependency targets must be active services in the same project.",
                    code="invalid_dependency_target",
                )

        # Deleting the old rows also deletes their declared graph edges (FK cascade).
        await self.deps.delete_for_service(service.id)
        added = [
            await self.deps.add(
                ServiceDependency(
                    service_id=service.id,
                    target_service_id=dep.target_service_id,
                    target_external=dep.target_external,
                    description=dep.description,
                )
            )
            for dep in deps
        ]
        # Each declared edge cites its dependency row; same transaction, so they cannot diverge.
        await self.edges.add_declared(added, at=datetime.now(UTC))
        self._audit(
            "service.dependencies_set",
            meta,
            "service",
            service.id,
            {"services": [str(t) for t in target_ids], "externals": externals},
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:  # a concurrent replace of the same set
            await self.session.rollback()
            raise ConflictError(
                "The dependencies were changed concurrently; retry.", code="concurrent_update"
            ) from exc
        return ServiceView(service, await self.deps.for_service(service.id))

    # ---- ingest API keys ----------------------------------------------------------------

    async def list_keys(
        self, service_id: uuid.UUID, *, limit: int, cursor: str | None
    ) -> tuple[list[ServiceApiKey], str | None]:
        service = await self.get(service_id)
        return await self.keys.list_for_service(service.id, limit=limit, cursor=cursor)

    async def create_key(
        self, service_id: uuid.UUID, *, name: str, meta: RequestMeta
    ) -> CreatedKey:
        service = await self._get_active(service_id)
        if await self.keys.count_active(service.id) >= MAX_ACTIVE_KEYS_PER_SERVICE:
            raise ConflictError(
                f"A service can have at most {MAX_ACTIVE_KEYS_PER_SERVICE} active keys.",
                code="too_many_keys",
            )
        generated = generate_key()
        key = await self.keys.add(
            ServiceApiKey(
                service_id=service.id,
                name=name,
                prefix=generated.prefix,
                secret_hash=generated.secret_hash,
                created_by=self.ctx.user_id,
            )
        )
        # The audit row carries the public prefix only, never the token or its hash.
        self._audit(
            "service.api_key_created",
            meta,
            "service_api_key",
            key.id,
            {"service_id": str(service.id), "prefix": generated.prefix, "name": name},
        )
        await self.session.commit()
        await self.session.refresh(key)
        return CreatedKey(key=key, token=generated.token)

    async def revoke_key(
        self, service_id: uuid.UUID, key_id: uuid.UUID, *, meta: RequestMeta
    ) -> ServiceApiKey:
        """Idempotent: revoking a revoked key returns it unchanged."""
        service = await self.get(service_id)
        key = await self.keys.get_for_service(service.id, key_id)
        if key is None:
            raise NotFoundError("API key not found.", code="api_key_not_found")
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            key.revoked_by = self.ctx.user_id
            self._audit(
                "service.api_key_revoked",
                meta,
                "service_api_key",
                key.id,
                {"service_id": str(service.id), "prefix": key.prefix},
            )
            await self.session.commit()
            await self.session.refresh(key)
        return key

    def _audit(
        self,
        action: str,
        meta: RequestMeta,
        target_type: str,
        target_id: uuid.UUID,
        metadata: dict[str, object],
    ) -> None:
        self.audit.record(
            action,
            meta,
            org_id=self.ctx.org_id,
            actor_user_id=self.ctx.user_id,
            target_type=target_type,
            target_id=target_id,
            metadata=dict(metadata),
        )


def _name_taken() -> ConflictError:
    return ConflictError(
        "An active service with this name already exists in this project and environment.",
        code="service_name_taken",
    )
