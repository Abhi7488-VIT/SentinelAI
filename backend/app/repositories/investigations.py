"""Investigation and evidence rows, tenant-scoped (invariant 4)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, update
from sqlalchemy.dialects.postgresql import insert

from app.models.investigation import ACTIVE_STATUSES, Evidence, Investigation
from app.repositories.base import TenantRepository


class InvestigationRepository(TenantRepository[Investigation]):
    model = Investigation

    async def try_lease(
        self, investigation_id: uuid.UUID, owner: str, ttl: timedelta
    ) -> Investigation | None:
        """Take (or keep) the lease of an active investigation; None if someone else holds a
        live lease or it is no longer active. `queued` becomes `running` on first lease."""
        free = (
            Investigation.lease_expires_at.is_(None)
            | (Investigation.lease_expires_at < func.now())
            | (Investigation.lease_owner == owner)
        )
        stmt = (
            update(Investigation)
            .where(
                Investigation.org_id == self.ctx.org_id,
                Investigation.id == investigation_id,
                Investigation.status.in_(ACTIVE_STATUSES),
                free,
            )
            .values(
                lease_owner=owner,
                lease_expires_at=func.now() + ttl,
                status="running",
                started_at=func.coalesce(Investigation.started_at, func.now()),
            )
            .returning(Investigation)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.scalars(stmt)).first()

    async def renew_lease(self, investigation_id: uuid.UUID, owner: str, ttl: timedelta) -> bool:
        stmt = (
            update(Investigation)
            .where(
                Investigation.org_id == self.ctx.org_id,
                Investigation.id == investigation_id,
                Investigation.lease_owner == owner,
            )
            .values(lease_expires_at=func.now() + ttl)
            .returning(Investigation.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.scalar(stmt)) is not None

    async def release_lease(self, investigation_id: uuid.UUID, owner: str) -> None:
        """Give the lease up without finishing (the run stays `running`, resumable at once)."""
        await self.session.execute(
            update(Investigation)
            .where(
                Investigation.org_id == self.ctx.org_id,
                Investigation.id == investigation_id,
                Investigation.lease_owner == owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )

    async def finish(
        self, investigation_id: uuid.UUID, owner: str, status: str, reasons: list[str]
    ) -> bool:
        """Terminal status, only by the lease holder; clears the lease."""
        stmt = (
            update(Investigation)
            .where(
                Investigation.org_id == self.ctx.org_id,
                Investigation.id == investigation_id,
                Investigation.lease_owner == owner,
            )
            .values(
                status=status,
                incomplete_reasons=reasons,
                finished_at=func.now(),
                lease_owner=None,
                lease_expires_at=None,
            )
            .returning(Investigation.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.scalar(stmt)) is not None


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

    async def many(self, investigation_id: uuid.UUID, ids: Sequence[str]) -> list[Evidence]:
        """These evidence rows of the investigation, in the given order (unknown ids skipped)."""
        if not ids:
            return []
        stmt = self.scoped().where(
            Evidence.investigation_id == investigation_id, Evidence.id.in_(list(set(ids)))
        )
        found = {e.id: e for e in (await self.session.scalars(stmt)).all()}
        return [found[i] for i in dict.fromkeys(ids) if i in found]

    async def count_for(self, investigation_id: uuid.UUID) -> int:
        stmt = (
            self.scoped()
            .where(Evidence.investigation_id == investigation_id)
            .with_only_columns(func.count())
        )
        return int(await self.session.scalar(stmt) or 0)

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
