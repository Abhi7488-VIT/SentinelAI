"""Ingest key lookup for OTLP authentication.

Documented exception to tenant scoping (ADR 0002): there is no TenantContext yet — the key *is*
the tenant credential, and the org/service are derived from the row it resolves to. The lookup is
by the key's public prefix (unique index) and returns only what authentication needs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.service import Service, ServiceApiKey


@dataclass(frozen=True, slots=True)
class KeyRecord:
    key_id: uuid.UUID
    org_id: uuid.UUID
    service_id: uuid.UUID
    service_name: str
    secret_hash: bytes
    revoked_at: datetime | None
    service_archived_at: datetime | None


async def find_key_by_prefix(session: AsyncSession, prefix: str) -> KeyRecord | None:
    stmt = (
        select(
            ServiceApiKey.id,
            ServiceApiKey.org_id,
            ServiceApiKey.service_id,
            Service.name,
            ServiceApiKey.secret_hash,
            ServiceApiKey.revoked_at,
            Service.archived_at,
        )
        .join(
            Service,
            (Service.org_id == ServiceApiKey.org_id) & (Service.id == ServiceApiKey.service_id),
        )
        .where(ServiceApiKey.prefix == prefix)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return KeyRecord(*row)
