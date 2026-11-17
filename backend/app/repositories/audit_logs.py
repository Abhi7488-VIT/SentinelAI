"""Audit logs: an insert-only writer and an org-scoped, read-only repository."""

from __future__ import annotations

from typing import NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.repositories.base import TenantRepository


class AuditLogWriter:
    """Insert-only. Not tenant-scoped because user-level events (login) have no org."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, entry: AuditLog) -> None:
        # Flushed with the caller's transaction so the audit row commits atomically
        # with the action it describes.
        self.session.add(entry)


class AuditLogRepository(TenantRepository[AuditLog]):
    """Read access to one org's audit trail. Mutations are refused: the log is append-only."""

    model = AuditLog

    async def add(self, obj: AuditLog) -> NoReturn:
        raise NotImplementedError("use AuditService.record()")

    async def delete(self, obj: AuditLog) -> NoReturn:
        raise NotImplementedError("audit logs are append-only")
