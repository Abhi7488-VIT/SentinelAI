"""Refresh token storage (hashes only)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: RefreshToken) -> RefreshToken:
        self.session.add(token)
        await self.session.flush()
        return token

    async def get_by_hash_for_update(self, token_hash: bytes) -> RefreshToken | None:
        """Row-locks the token so concurrent refreshes of the same token serialize."""
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        return (await self.session.scalars(stmt)).first()

    async def revoke_family(self, family_id: uuid.UUID, *, reason: str, now: datetime) -> int:
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
