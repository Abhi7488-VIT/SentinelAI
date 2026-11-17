"""Repository base classes.

`TenantRepository` is the single enforcement point for tenant isolation (CLAUDE.md invariant 4):
every query it builds is filtered by the `org_id` of the `TenantContext` it was constructed
with, and every row it creates is stamped with that `org_id`. Services never filter by org.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, Select, Uuid, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import (
    MAX_LIMIT,
    Cursor,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from app.core.tenancy import TenantContext
from app.db.base import Base


class TenantIsolationError(RuntimeError):
    """A programming error: an object from another tenant reached a scoped repository."""


async def keyset_page[M: Base](
    session: AsyncSession,
    stmt: Select[tuple[M]],
    model: type[M],
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[M], str | None]:
    """Run `stmt` ordered by (created_at DESC, id DESC) with an opaque keyset cursor."""
    limit = max(1, min(limit, MAX_LIMIT))
    created_col = model.__table__.c["created_at"]
    id_col = model.__table__.c["id"]
    if cursor is not None:
        position = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(created_col, id_col)
            < tuple_(literal(position.created_at), literal(_cursor_id(position, id_col)))
        )
    stmt = stmt.order_by(created_col.desc(), id_col.desc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id)  # type: ignore[attr-defined]
    return rows, next_cursor


def _cursor_id(position: Cursor, id_col: ColumnElement[Any]) -> uuid.UUID | int:
    try:
        return uuid.UUID(position.id) if isinstance(id_col.type, Uuid) else int(position.id)
    except ValueError as exc:
        raise InvalidCursorError() from exc


class TenantRepository[M: Base]:
    model: ClassVar[type[Base]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = getattr(cls, "model", None)
        if model is not None and "org_id" not in model.__table__.c:
            raise TypeError(f"{cls.__name__}.model {model.__name__} has no org_id column")

    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.session = session
        self.ctx = ctx

    @property
    def _model(self) -> type[M]:
        return self.model  # type: ignore[return-value]

    def _org_col(self) -> ColumnElement[Any]:
        return self._model.__table__.c["org_id"]

    def scoped(self) -> Select[tuple[M]]:
        """SELECT of this model restricted to the current tenant. Extend, never replace."""
        return select(self._model).where(self._org_col() == self.ctx.org_id)

    async def get(self, id_: uuid.UUID) -> M | None:
        stmt = self.scoped().where(self._model.__table__.c["id"] == id_)
        return (await self.session.scalars(stmt)).first()

    async def add(self, obj: M) -> M:
        obj.org_id = self.ctx.org_id  # type: ignore[attr-defined]
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def delete(self, obj: M) -> None:
        self._assert_owned(obj)
        await self.session.delete(obj)
        await self.session.flush()

    async def list_page(
        self,
        *,
        limit: int,
        cursor: str | None,
        where: Sequence[ColumnElement[bool]] = (),
    ) -> tuple[list[M], str | None]:
        stmt = self.scoped()
        for clause in where:
            stmt = stmt.where(clause)
        return await keyset_page(self.session, stmt, self._model, limit=limit, cursor=cursor)

    def _assert_owned(self, obj: M) -> None:
        if obj.org_id != self.ctx.org_id:  # type: ignore[attr-defined]
            raise TenantIsolationError(f"{type(obj).__name__} does not belong to this tenant")
