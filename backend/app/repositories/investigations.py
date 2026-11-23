"""Investigation and evidence rows, tenant-scoped (invariant 4)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, delete, func, update
from sqlalchemy.dialects.postgresql import insert

from app.models.investigation import (
    ACTIVE_STATUSES,
    Evidence,
    Hypothesis,
    HypothesisEvidence,
    Investigation,
    VerificationCheck,
)
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


@dataclass(frozen=True, slots=True)
class HypothesisWrite:
    proposed_rank: int
    rank: int
    is_primary: bool
    category: str
    subject_service_ids: list[str]
    statement: str
    status: str
    status_reason: str
    checks_passed: int
    checks_applicable: int
    model_confidence: float
    rejected_citations: list[str]
    missing_evidence: list[str]
    supporting: list[str]
    contradicting: list[str]
    checks: Sequence[tuple[str, str, str, list[str]]]  # (name, result, detail, evidence ids)


class HypothesisRepository(TenantRepository[Hypothesis]):
    model = Hypothesis

    async def replace_for_investigation(
        self, investigation_id: uuid.UUID, items: Sequence[HypothesisWrite]
    ) -> list[Hypothesis]:
        """Delete this investigation's hypotheses (cascading citations and checks) and write the
        new set, in the caller's transaction: a re-run of verification replaces, never adds."""
        await self.session.execute(
            delete(Hypothesis).where(
                Hypothesis.org_id == self.ctx.org_id,
                Hypothesis.investigation_id == investigation_id,
            )
        )
        out: list[Hypothesis] = []
        for it in items:
            h = Hypothesis(
                org_id=self.ctx.org_id,
                investigation_id=investigation_id,
                proposed_rank=it.proposed_rank,
                rank=it.rank,
                is_primary=it.is_primary,
                category=it.category,
                subject_service_ids=it.subject_service_ids,
                statement=it.statement,
                status=it.status,
                status_reason=it.status_reason[:200],
                checks_passed=it.checks_passed,
                checks_applicable=it.checks_applicable,
                model_confidence=it.model_confidence,
                rejected_citations=it.rejected_citations,
                missing_evidence=it.missing_evidence,
            )
            self.session.add(h)
            await self.session.flush()
            for role, ids in (("supporting", it.supporting), ("contradicting", it.contradicting)):
                for ev in dict.fromkeys(ids):
                    self.session.add(
                        HypothesisEvidence(
                            org_id=self.ctx.org_id,
                            investigation_id=investigation_id,
                            hypothesis_id=h.id,
                            evidence_id=ev,
                            role=role,
                        )
                    )
            for name, result, detail, ids in it.checks:
                self.session.add(
                    VerificationCheck(
                        org_id=self.ctx.org_id,
                        hypothesis_id=h.id,
                        name=name,
                        result=result,
                        detail=detail[:500],
                        evidence_ids=list(ids),
                    )
                )
            out.append(h)
        await self.session.flush()
        return out

    async def for_investigation(self, investigation_id: uuid.UUID) -> list[Hypothesis]:
        stmt = (
            self.scoped()
            .where(Hypothesis.investigation_id == investigation_id)
            .order_by(Hypothesis.rank, Hypothesis.id)
        )
        return list((await self.session.scalars(stmt)).all())


class HypothesisEvidenceRepository(TenantRepository[HypothesisEvidence]):
    model = HypothesisEvidence
    id_column = "hypothesis_id"

    async def for_hypotheses(self, ids: Sequence[uuid.UUID]) -> list[HypothesisEvidence]:
        if not ids:
            return []
        stmt = (
            self.scoped()
            .where(HypothesisEvidence.hypothesis_id.in_(list(ids)))
            .order_by(
                HypothesisEvidence.hypothesis_id,
                HypothesisEvidence.role,
                HypothesisEvidence.evidence_id,
            )
        )
        return list((await self.session.scalars(stmt)).all())


class VerificationCheckRepository(TenantRepository[VerificationCheck]):
    model = VerificationCheck

    async def for_hypotheses(self, ids: Sequence[uuid.UUID]) -> list[VerificationCheck]:
        if not ids:
            return []
        stmt = (
            self.scoped()
            .where(VerificationCheck.hypothesis_id.in_(list(ids)))
            .order_by(VerificationCheck.hypothesis_id, VerificationCheck.id)
        )
        return list((await self.session.scalars(stmt)).all())
