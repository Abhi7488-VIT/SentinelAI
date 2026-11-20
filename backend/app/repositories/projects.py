from __future__ import annotations

from sqlalchemy import ColumnElement, exists, select

from app.models.project import Project
from app.repositories.base import TenantRepository


class ProjectRepository(TenantRepository[Project]):
    model = Project

    async def slug_exists(self, slug: str) -> bool:
        stmt = select(exists(self.scoped().where(Project.slug == slug).subquery()))
        return bool(await self.session.scalar(stmt))

    async def list_filtered(
        self, *, limit: int, cursor: str | None, include_archived: bool
    ) -> tuple[list[Project], str | None]:
        where: list[ColumnElement[bool]] = []
        if not include_archived:
            where.append(Project.archived_at.is_(None))
        return await self.list_page(limit=limit, cursor=cursor, where=where)
