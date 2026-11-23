"""Hypotheses and verification persisted by the graph (Phase 4 T4), on seeded telemetry."""

# ruff: noqa: F401, F811  (pytest fixtures imported from sibling modules, used as arguments)

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.exc import IntegrityError

from app.core.tenancy import SystemScope
from app.llm.providers.fake import FakeStep, Respond
from app.llm.types import ProviderRequest
from app.repositories.investigations import (
    HypothesisEvidenceRepository,
    HypothesisRepository,
    VerificationCheckRepository,
)
from tests.integration.factories import register
from tests.integration.telemetry_support import SM, rows, sql
from tests.integration.test_investigation_graph import (
    EV,
    FAKE_ID,
    checkpointer,
    gw,
    run,
    scripted,
)
from tests.integration.test_investigation_tools import partitions, t0, world


def analyst_blaming(subject: str) -> Any:
    """A model that names `subject` (resource_exhaustion, low confidence) and a vague `other`
    hypothesis with confidence 1.0, citing every evidence id it was shown plus one made up."""

    def respond(req: ProviderRequest) -> FakeStep:
        ids = EV.findall("\n".join(req.user_blocks))
        base = {
            "subject_service_ids": [subject],
            "statement": "s",
            "supporting_evidence_ids": [*ids, FAKE_ID],
            "contradicting_evidence_ids": [],
            "missing_evidence": ["db server metrics"],
        }
        return Respond(
            {
                "hypotheses": [
                    {**base, "category": "other", "confidence": 1.0},
                    {**base, "category": "resource_exhaustion", "confidence": 0.2},
                ]
            }
        )

    return respond


async def test_verification_confirms_on_direct_evidence_and_persists(
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    orders = str(gw["orders"])
    out = await run(sessionmaker, gw, checkpointer, scripted({"analyst": analyst_blaming(orders)}))
    first, second = out.state["hypotheses"]
    # Ranked by verification, not by the model's confidence (1.0 for the vague one).
    assert (first["category"], first["status"], first["is_primary"]) == (
        "resource_exhaustion",
        "confirmed",
        True,
    )
    assert [c["result"] for c in first["checks"]] == ["pass", "pass", "pass"]
    assert (second["category"], second["status"], second["is_primary"]) == (
        "other",
        "inconclusive",
        False,
    )
    assert first["rejected_citations"] == [FAKE_ID]
    assert "verification_not_run" not in out.reasons

    scope = SystemScope(gw["org"], "t")
    async with sessionmaker() as s:
        hyps = await HypothesisRepository(s, scope).for_investigation(gw["inv"])
        cites = await HypothesisEvidenceRepository(s, scope).for_hypotheses([h.id for h in hyps])
        checks = await VerificationCheckRepository(s, scope).for_hypotheses([h.id for h in hyps])
    assert [(h.rank, h.category, h.status, h.is_primary) for h in hyps] == [
        (1, "resource_exhaustion", "confirmed", True),
        (2, "other", "inconclusive", False),
    ]
    assert hyps[0].model_confidence == 0.2
    assert hyps[0].checks_passed == 3
    assert hyps[0].rejected_citations == [FAKE_ID]
    stored = {
        r["id"]
        for r in await rows(
            sessionmaker, "SELECT id FROM evidence WHERE investigation_id = :i", i=gw["inv"]
        )
    }
    assert {c.evidence_id for c in cites} <= stored
    assert FAKE_ID not in {c.evidence_id for c in cites}
    assert [c.name for c in checks if c.hypothesis_id == hyps[0].id] == [
        "saturation_or_timeout_signature_in_subject",
        "subject_onset_not_after_first_downstream_symptom",
        "subject_outbound_or_db_error_rate_rose",
    ]
    for c in checks:
        assert set(c.evidence_ids) <= stored


async def test_contradicting_citation_keeps_it_probable(
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    orders = str(gw["orders"])

    def respond(req: ProviderRequest) -> FakeStep:
        ids = EV.findall("\n".join(req.user_blocks))
        h = {
            "category": "resource_exhaustion",
            "subject_service_ids": [orders],
            "statement": "s",
            "supporting_evidence_ids": ids,
            "contradicting_evidence_ids": ids[:1],
            "missing_evidence": [],
            "confidence": 1.0,
        }
        return Respond({"hypotheses": [h, {**h, "category": "other"}]})

    out = await run(sessionmaker, gw, checkpointer, scripted({"analyst": respond}))
    assert out.state["hypotheses"][0]["status"] == "probable"
    assert out.state["hypotheses"][0]["status_reason"] == "contradicting evidence cited"


async def test_citation_fk_rejects_dangling_and_cross_investigation_ids(
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    orders = str(gw["orders"])
    await run(sessionmaker, gw, checkpointer, scripted({"analyst": analyst_blaming(orders)}))
    (h,) = await rows(
        sessionmaker,
        "SELECT id FROM hypotheses WHERE investigation_id = :i AND is_primary",
        i=gw["inv"],
    )
    insert = (
        "INSERT INTO hypothesis_evidence (org_id, investigation_id, hypothesis_id, evidence_id,"
        " role) VALUES (:o, :i, :h, :e, 'supporting')"
    )
    with pytest.raises(IntegrityError, match="fk_hypothesis_evidence_evidence"):
        await sql(sessionmaker, insert, o=gw["org"], i=gw["inv"], h=h["id"], e="ev_aaaaaaaaaaaa")

    # Evidence of another investigation (same org) cannot be cited either.
    await sql(
        sessionmaker, "UPDATE investigations SET status = 'completed' WHERE id = :i", i=gw["inv"]
    )
    (other,) = await rows(
        sessionmaker,
        "INSERT INTO investigations (org_id, incident_id, status) VALUES (:o, :i, 'queued')"
        " RETURNING id",
        o=gw["org"],
        i=gw["incident"],
    )
    await run(sessionmaker, gw, checkpointer, scripted(), inv=other["id"])
    (foreign_ev,) = await rows(
        sessionmaker, "SELECT id FROM evidence WHERE investigation_id = :i LIMIT 1", i=other["id"]
    )
    with pytest.raises(IntegrityError, match="fk_hypothesis_evidence_evidence"):
        await sql(sessionmaker, insert, o=gw["org"], i=gw["inv"], h=h["id"], e=foreign_ev["id"])


async def test_rerun_of_verification_replaces_rows(
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    orders = str(gw["orders"])
    await run(sessionmaker, gw, checkpointer, scripted({"analyst": analyst_blaming(orders)}))
    before = await rows(
        sessionmaker,
        "SELECT proposed_rank, rank, status, checks_passed FROM hypotheses"
        " WHERE investigation_id = :i ORDER BY rank",
        i=gw["inv"],
    )
    scope = SystemScope(gw["org"], "t")
    async with sessionmaker() as s:
        repo = HypothesisRepository(s, scope)
        existing = await repo.for_investigation(gw["inv"])
        assert len(existing) == 2
        # Simulate the verification node running again after a crash before the next checkpoint.
        from app.repositories.investigations import HypothesisWrite

        writes = [
            HypothesisWrite(
                proposed_rank=h.proposed_rank,
                rank=h.rank,
                is_primary=h.is_primary,
                category=h.category,
                subject_service_ids=h.subject_service_ids,
                statement=h.statement,
                status=h.status,
                status_reason=h.status_reason,
                checks_passed=h.checks_passed,
                checks_applicable=h.checks_applicable,
                model_confidence=h.model_confidence,
                rejected_citations=h.rejected_citations,
                missing_evidence=h.missing_evidence,
                supporting=[],
                contradicting=[],
                checks=[],
            )
            for h in existing
        ]
        await repo.replace_for_investigation(gw["inv"], writes)
        await s.commit()
    after = await rows(
        sessionmaker,
        "SELECT proposed_rank, rank, status, checks_passed FROM hypotheses"
        " WHERE investigation_id = :i ORDER BY rank",
        i=gw["inv"],
    )
    assert after == before


async def test_hypotheses_are_tenant_scoped(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    await run(sessionmaker, gw, checkpointer, scripted())
    other = await register(client, "Other", "Other Org")
    scope = SystemScope(uuid.UUID(other.org_id), "t")
    async with sessionmaker() as s:
        assert await HypothesisRepository(s, scope).for_investigation(gw["inv"]) == []
        ids = [
            r["id"]
            for r in await rows(
                sessionmaker, "SELECT id FROM hypotheses WHERE investigation_id = :i", i=gw["inv"]
            )
        ]
        assert ids
        assert await HypothesisEvidenceRepository(s, scope).for_hypotheses(ids) == []
        assert await VerificationCheckRepository(s, scope).for_hypotheses(ids) == []
