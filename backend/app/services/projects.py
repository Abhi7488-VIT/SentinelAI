"""Projects within an organization. All access goes through ProjectRepository (tenant-scoped)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.slugs import slugify, with_random_suffix
from app.core.tenancy import TenantContext
from app.models.project import Project
from app.repositories.projects import ProjectRepository
from app.repositories.services import ServiceApiKeyRepository, ServiceRepository
from app.services.audit import AuditService, RequestMeta


class ProjectService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.session = session
        self.ctx = ctx
        self.projects = ProjectRepository(session, ctx)
        self.audit = AuditService(session)

    async def list(
        self, *, limit: int, cursor: str | None, include_archived: bool = False
    ) -> tuple[list[Project], str | None]:
        return await self.projects.list_filtered(
            limit=limit, cursor=cursor, include_archived=include_archived
        )

    async def get(self, project_id: uuid.UUID) -> Project:
        project = await self.projects.get(project_id)
        if project is None:
            raise NotFoundError("Project not found.", code="project_not_found")
        return project

    async def get_active(self, project_id: uuid.UUID) -> Project:
        """The project, or 409 if it is archived (archived projects are read-only)."""
        project = await self.get(project_id)
        if project.archived_at is not None:
            raise ConflictError("The project is archived.", code="project_archived")
        return project

    async def create(
        self, *, name: str, slug: str | None, description: str | None, meta: RequestMeta
    ) -> Project:
        if slug is not None:
            if await self.projects.slug_exists(slug):
                raise _slug_taken()
        else:
            slug = slugify(name, fallback="project")
            if await self.projects.slug_exists(slug):
                slug = with_random_suffix(slug)
        try:
            project = await self.projects.add(
                Project(name=name, slug=slug, description=description, created_by=self.ctx.user_id)
            )
            self._audit("project.created", meta, project.id, {"slug": slug})
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _slug_taken() from exc
        await self.session.refresh(project)
        return project

    async def update(
        self,
        project_id: uuid.UUID,
        *,
        changes: dict[str, str | None],
        meta: RequestMeta,
    ) -> Project:
        """`changes` holds only fields the client sent (name, description)."""
        project = await self.get_active(project_id)
        applied = {k: v for k, v in changes.items() if getattr(project, k) != v}
        if applied:
            for key, value in applied.items():
                setattr(project, key, value)
            self._audit("project.updated", meta, project.id, {"fields": sorted(applied)})
            await self.session.commit()
            await self.session.refresh(project)
        return project

    async def archive(self, project_id: uuid.UUID, *, meta: RequestMeta) -> None:
        """Archive the project, its services, and revoke their ingest keys (one transaction).

        Idempotent: archiving an archived project is a no-op. Nothing is hard-deleted, because
        services, telemetry and incidents hang off projects.
        """
        project = await self.get(project_id)
        if project.archived_at is not None:
            return
        now = datetime.now(UTC)
        project.archived_at = now
        services = await ServiceRepository(self.session, self.ctx).active_in_project(project_id)
        for service in services:
            service.archived_at = now
        revoked = await ServiceApiKeyRepository(self.session, self.ctx).revoke_all_for_services(
            [s.id for s in services], at=now, by=self.ctx.user_id
        )
        self._audit(
            "project.archived",
            meta,
            project_id,
            {
                "slug": project.slug,
                "services_archived": len(services),
                "keys_revoked": len(revoked),
            },
        )
        await self.session.commit()

    def _audit(
        self, action: str, meta: RequestMeta, project_id: uuid.UUID, metadata: dict[str, object]
    ) -> None:
        self.audit.record(
            action,
            meta,
            org_id=self.ctx.org_id,
            actor_user_id=self.ctx.user_id,
            target_type="project",
            target_id=project_id,
            metadata=dict(metadata),
        )


def _slug_taken() -> ConflictError:
    return ConflictError("A project with this slug already exists.", code="slug_taken")
