"""Create a minimal project → incident → investigation chain for an org (SQL, test-only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def create_investigation(
    session: AsyncSession, org_id: uuid.UUID, *, status: str = "queued"
) -> uuid.UUID:
    now = datetime.now(UTC)
    project = await session.scalar(
        text("INSERT INTO projects (org_id, name, slug) VALUES (:o, 'P', :slug) RETURNING id"),
        {"o": org_id, "slug": f"p-{uuid.uuid4().hex[:8]}"},
    )
    number = await session.scalar(
        text("SELECT coalesce(max(number), 0) + 1 FROM incidents WHERE org_id = :o"),
        {"o": org_id},
    )
    incident = await session.scalar(
        text(
            "INSERT INTO incidents (org_id, project_id, number, title, status, severity,"
            " started_at, detected_at, last_activity_at) VALUES (:o, :p, :n, 'Test incident',"
            " 'open', 'warning', :s, :s, :s) RETURNING id"
        ),
        {"o": org_id, "p": project, "n": number, "s": now - timedelta(minutes=5)},
    )
    inv = await session.scalar(
        text(
            "INSERT INTO investigations (org_id, incident_id, status) VALUES (:o, :i, :st)"
            " RETURNING id"
        ),
        {"o": org_id, "i": incident, "st": status},
    )
    await session.commit()
    assert isinstance(inv, uuid.UUID)
    return inv
