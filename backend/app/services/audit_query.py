"""Read access to an organization's audit trail (admin+)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.models.audit_log import AuditLog
from app.repositories.audit_logs import AuditLogRepository


class AuditQueryService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.logs = AuditLogRepository(session, ctx)

    async def list(self, *, limit: int, cursor: str | None) -> tuple[list[AuditLog], str | None]:
        return await self.logs.list_page(limit=limit, cursor=cursor)
