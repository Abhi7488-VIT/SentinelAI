"""Service registry repositories. All tenant-scoped (TenantRepository filters by org_id)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, delete, func, select, update

from app.models.service import Service, ServiceApiKey, ServiceDependency
from app.repositories.base import TenantRepository

# Deterministic output order. Rows written in one transaction share created_at (now() is the
# transaction start), so time cannot order them: service targets first, then externals by name.
_DEPENDENCY_ORDER = (
    ServiceDependency.target_service_id.is_(None),
    ServiceDependency.target_external,
    ServiceDependency.target_service_id,
)


class ServiceRepository(TenantRepository[Service]):
    model = Service

    async def list_filtered(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: uuid.UUID | None,
        environment: str | None,
        include_archived: bool,
    ) -> tuple[list[Service], str | None]:
        where: list[ColumnElement[bool]] = []
        if project_id is not None:
            where.append(Service.project_id == project_id)
        if environment is not None:
            where.append(Service.environment == environment)
        if not include_archived:
            where.append(Service.archived_at.is_(None))
        return await self.list_page(limit=limit, cursor=cursor, where=where)

    async def active_name_exists(
        self, *, project_id: uuid.UUID, environment: str, name: str, exclude_id: uuid.UUID | None
    ) -> bool:
        stmt = self.scoped().where(
            Service.project_id == project_id,
            Service.environment == environment,
            Service.name == name,
            Service.archived_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(Service.id != exclude_id)
        return (await self.session.scalars(stmt.limit(1))).first() is not None

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Service]:
        if not ids:
            return []
        stmt = self.scoped().where(Service.id.in_(ids))
        return list((await self.session.scalars(stmt)).all())

    async def active_in_project(self, project_id: uuid.UUID) -> list[Service]:
        stmt = self.scoped().where(Service.project_id == project_id, Service.archived_at.is_(None))
        return list((await self.session.scalars(stmt)).all())


class ServiceDependencyRepository(TenantRepository[ServiceDependency]):
    model = ServiceDependency

    async def for_service(self, service_id: uuid.UUID) -> list[ServiceDependency]:
        stmt = (
            self.scoped()
            .where(ServiceDependency.service_id == service_id)
            .order_by(*_DEPENDENCY_ORDER)
        )
        return list((await self.session.scalars(stmt)).all())

    async def for_services(
        self, service_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[ServiceDependency]]:
        out: dict[uuid.UUID, list[ServiceDependency]] = {sid: [] for sid in service_ids}
        if not service_ids:
            return out
        stmt = (
            self.scoped()
            .where(ServiceDependency.service_id.in_(service_ids))
            .order_by(*_DEPENDENCY_ORDER)
        )
        for dep in (await self.session.scalars(stmt)).all():
            out[dep.service_id].append(dep)
        return out

    async def delete_for_service(self, service_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(ServiceDependency).where(
                ServiceDependency.org_id == self.ctx.org_id,
                ServiceDependency.service_id == service_id,
            )
        )


class ServiceApiKeyRepository(TenantRepository[ServiceApiKey]):
    model = ServiceApiKey

    async def get_for_service(
        self, service_id: uuid.UUID, key_id: uuid.UUID
    ) -> ServiceApiKey | None:
        stmt = self.scoped().where(
            ServiceApiKey.id == key_id, ServiceApiKey.service_id == service_id
        )
        return (await self.session.scalars(stmt)).first()

    async def list_for_service(
        self, service_id: uuid.UUID, *, limit: int, cursor: str | None
    ) -> tuple[list[ServiceApiKey], str | None]:
        return await self.list_page(
            limit=limit, cursor=cursor, where=[ServiceApiKey.service_id == service_id]
        )

    async def count_active(self, service_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ServiceApiKey)
            .where(
                ServiceApiKey.org_id == self.ctx.org_id,
                ServiceApiKey.service_id == service_id,
                ServiceApiKey.revoked_at.is_(None),
            )
        )
        return int(await self.session.scalar(stmt) or 0)

    async def revoke_all_for_services(
        self, service_ids: Sequence[uuid.UUID], *, at: datetime, by: uuid.UUID
    ) -> list[str]:
        """Revoke every active key of these services; returns the revoked prefixes."""
        if not service_ids:
            return []
        stmt = (
            update(ServiceApiKey)
            .where(
                ServiceApiKey.org_id == self.ctx.org_id,
                ServiceApiKey.service_id.in_(service_ids),
                ServiceApiKey.revoked_at.is_(None),
            )
            .values(revoked_at=at, revoked_by=by)
            .returning(ServiceApiKey.prefix)
        )
        return list((await self.session.scalars(stmt)).all())
