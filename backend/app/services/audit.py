"""Audit trail recording. Never persists secrets: metadata is redacted before insert."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, redact_mapping
from app.models.audit_log import AuditLog
from app.repositories.audit_logs import AuditLogWriter

logger = get_logger("app.audit")


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip: str | None
    user_agent: str | None
    request_id: str | None


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._writer = AuditLogWriter(session)

    def record(
        self,
        action: str,
        meta: RequestMeta,
        *,
        org_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: str | uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._writer.add(
            AuditLog(
                org_id=org_id,
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
                metadata_=redact_mapping(metadata or {}),
            )
        )
        logger.info(
            "audit.recorded",
            action=action,
            org_id=str(org_id) if org_id else None,
            actor_user_id=str(actor_user_id) if actor_user_id else None,
        )
