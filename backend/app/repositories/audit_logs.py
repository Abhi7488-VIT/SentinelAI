"""Audit log writer (insert-only). Org-scoped reads live in a TenantRepository (T6)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


class AuditLogWriter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, entry: AuditLog) -> None:
        # Flushed with the caller's transaction so the audit row commits atomically
        # with the action it describes.
        self.session.add(entry)
