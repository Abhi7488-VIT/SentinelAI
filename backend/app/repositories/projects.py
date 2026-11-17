from __future__ import annotations

from sqlalchemy import exists, select

from app.models.project import Project
from app.repositories.base import TenantRepository


class ProjectRepository(TenantRepository[Project]):
    model = Project

    async def slug_exists(self, slug: str) -> bool:
        stmt = select(exists(self.scoped().where(Project.slug == slug).subquery()))
        return bool(await self.session.scalar(stmt))
