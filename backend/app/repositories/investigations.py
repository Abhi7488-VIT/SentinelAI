"""Investigation and evidence rows, tenant-scoped (invariant 4)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.dialects.postgresql import insert

from app.models.investigation import Evidence, Investigation
from app.repositories.base import TenantRepository


class InvestigationRepository(TenantRepository[Investigation]):
    model = Investigation


class EvidenceRepository(TenantRepository[Evidence]):
    model = Evidence

    async def insert_once(self, values: dict[str, Any]) -> tuple[Evidence, bool]:
        """Insert unless `(investigation_id, dedup_key)` exists; return (row, created).

        The investigation must belong to this org: the composite FK rejects anything else.
        """
        stmt = (
            insert(Evidence)
            .values(**values, org_id=self.ctx.org_id)
            .on_conflict_do_nothing(constraint="uq_evidence_dedup")
            .returning(Evidence.id)
        )
        new_id = await self.session.scalar(stmt)
        created = new_id is not None
        found = await self.session.scalars(
            self.scoped()
            .where(
                Evidence.investigation_id == values["investigation_id"],
                Evidence.dedup_key == values["dedup_key"],
            )
            .execution_options(populate_existing=True)
        )
        row = found.one()
        return row, created

    async def get_in_investigation(
        self, investigation_id: uuid.UUID, evidence_id: str
    ) -> Evidence | None:
        stmt = self.scoped().where(
            Evidence.investigation_id == investigation_id, Evidence.id == evidence_id
        )
        return (await self.session.scalars(stmt)).first()

    async def existing_ids(self, investigation_id: uuid.UUID, ids: Sequence[str]) -> set[str]:
        """Which of `ids` exist for this investigation (citation check, invariant 1)."""
        if not ids:
            return set()
        stmt = self.scoped().where(
            Evidence.investigation_id == investigation_id, Evidence.id.in_(list(set(ids)))
        )
        return {e.id for e in (await self.session.scalars(stmt)).all()}

    async def list_for_investigation(
        self,
        investigation_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        kind: str | None = None,
        service_id: uuid.UUID | None = None,
    ) -> tuple[list[Evidence], str | None]:
        where: list[ColumnElement[bool]] = [Evidence.investigation_id == investigation_id]
        if kind is not None:
            where.append(Evidence.kind == kind)
        if service_id is not None:
            where.append(Evidence.service_ids.contains([str(service_id)]))
        return await self.list_page(limit=limit, cursor=cursor, where=where)
