"""Users are global (not tenant-owned); lookups are by id or normalized email."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        return (await self.session.scalars(select(User).where(User.email == email))).first()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user
