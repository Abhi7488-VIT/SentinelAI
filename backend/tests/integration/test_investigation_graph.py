"""LangGraph workflow (Phase 4 T3) end to end with the FakeProvider and the Postgres checkpointer.

Reuses the generic seeded telemetry of `test_investigation_tools` (gateway -> orders -> DB).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import Settings
from app.core.tenancy import SystemScope
from app.investigation.checkpoint import open_checkpointer, setup_checkpointer
from app.investigation.runner import CommittingUsageSink, RunOutcome, run_investigation
from app.llm.client import LLMClient
from app.llm.pricing import DEFAULT_PRICES
from app.llm.providers.fake import FakeProvider, FakeStep, Respond
from app.llm.types import ProviderRequest
from app.repositories.investigations import InvestigationRepository
from tests.integration.telemetry_support import SM, rows, sql
from tests.integration.test_investigation_tools import (  # noqa: F401  (pytest fixtures)
    partitions,
    t0,
    world,
)

EV = re.compile(r'id="(ev_[a-z2-7]{12})"')
UUID_RE = re.compile(r"^- ([0-9a-f-]{36}):", re.MULTILINE)
FAKE_ID = "ev_zzzzzzzzzzzz"
FOREIGN_SERVICE = str(uuid.UUID(int=7))


class CrashError(Exception):
    """Simulated worker death inside a node."""


def prompt_of(req: ProviderRequest) -> str:
    m = re.match(r"SentinelAI prompt: (\w+) v\d+", req.system)
    assert m, req.system[:80]
    return m.group(1)


def candidates_in(req: ProviderRequest) -> list[str]:
    block = next(b for b in req.user_blocks if b.startswith("Candidate services"))
    return UUID_RE.findall(block)


def scripted(
    overrides: dict[str, Callable[[ProviderRequest], FakeStep]] | None = None,
) -> FakeProvider:
    """A well-behaved fake model, except that it also proposes one foreign service and cites one
    made-up evidence id: both must be dropped by the engine."""
    overrides = overrides or {}

    def respond(req: ProviderRequest) -> FakeStep:
        name = prompt_of(req)
        if name in overrides:
            return overrides[name](req)
        ids = EV.findall("\n".join(req.user_blocks))
        if name == "planner":
            return Respond(
                {"focus_service_ids": [*candidates_in(req), FOREIGN_SERVICE], "rationale": "r"}
            )
        if name in ("telemetry", "dependency"):
            return Respond(
                {
                    "claims": [
                        {"statement": f"{name} change seen", "evidence_ids": [*ids[:2], FAKE_ID]},
                        {"statement": "only a made-up citation", "evidence_ids": [FAKE_ID]},
                    ]
                }
            )
        subject = candidates_in(req)[0]
        hyp = {
            "category": "resource_exhaustion",
            "subject_service_ids": [subject, FOREIGN_SERVICE],
            "statement": "pool ran out",
            "supporting_evidence_ids": [*ids[:3], FAKE_ID],
            "contradicting_evidence_ids": [],
            "missing_evidence": [],
            "confidence": 1.0,
        }
        return Respond({"hypotheses": [hyp, {**hyp, "category": "other", "confidence": 0.2}]})

    return FakeProvider(responder=respond)


def llm(sm: SM, org: uuid.UUID, provider: FakeProvider) -> LLMClient:
    return LLMClient(
        provider,
        CommittingUsageSink(sm, SystemScope(org, "investigation")),
        DEFAULT_PRICES,
        timeout_s=5,
        max_invalid_retries=1,
        max_transport_retries=0,
        max_input_tokens=30_000,
    )


@pytest.fixture
async def checkpointer(test_settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    url = test_settings.database_url.get_secret_value()
    await setup_checkpointer(url)
    async with open_checkpointer(url) as cp:
        yield cp


@pytest.fixture
async def gw(sessionmaker: SM, world: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    """`world` plus the incident's affected services (what correlation would have stored)."""
    for svc in (world["gw"], world["orders"]):
        await sql(
            sessionmaker,
            "INSERT INTO incident_services (org_id, incident_id, service_id) VALUES (:o, :i, :s)",
            o=world["org"],
            i=world["incident"],
            s=svc,
        )
    return world


async def run(
    sm: SM,
    w: dict[str, Any],
    cp: AsyncPostgresSaver,
    provider: FakeProvider,
    *,
    inv: uuid.UUID | None = None,
    owner: str = "worker-1",
    hooks: dict[str, Any] | None = None,
) -> RunOutcome:
    return await run_investigation(
        org_id=w["org"],
        investigation_id=inv or w["inv"],
        sessionmaker=sm,
        llm=llm(sm, w["org"], provider),
        checkpointer=cp,
        owner=owner,
        hooks=hooks,
    )


async def evidence_rows(sm: SM, inv: uuid.UUID) -> list[dict[str, Any]]:
    return await rows(
        sm, "SELECT id, kind, dedup_key FROM evidence WHERE investigation_id = :i", i=inv
    )


async def test_end_to_end_with_fake_provider(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    provider = scripted()
    out = await run(sessionmaker, gw, checkpointer, provider)

    assert out.status == "incomplete"  # verification/report arrive in T4/T7: never "completed"
    assert {"verification_not_run", "report_not_generated"} <= set(out.reasons)
    assert out.resumed is False
    state = out.state
    assert state["stages"][0] == "planner"
    assert set(state["stages"][1:3]) == {"telemetry", "dependency"}
    assert state["stages"][3:] == ["analyst", "verification", "report"]
    assert state["plan_source"] == "llm"
    assert set(state["focus"]) == {str(gw["gw"]), str(gw["orders"])}

    # Every id in state resolves; the made-up id and the foreign service were dropped + recorded.
    stored = {r["id"] for r in await evidence_rows(sessionmaker, gw["inv"])}
    assert set(state["evidence_ids"]) == stored
    for f in state["findings"]:
        assert f["evidence_ids"]
        assert set(f["evidence_ids"]) <= stored
    assert "only a made-up citation" not in json.dumps(state["findings"])
    assert {"node": "planner", "kind": "service", "value": FOREIGN_SERVICE} in state["rejected"]
    assert {"node": "analyst", "kind": "citation", "value": FAKE_ID} in state["rejected"]
    h1 = state["hypotheses"][0]
    assert len(state["hypotheses"]) == 2
    assert h1["subject_service_ids"] == [str(gw["gw"])] or h1["subject_service_ids"] == [
        str(gw["orders"])
    ]
    assert set(h1["supporting_evidence_ids"]) <= stored
    assert h1["rejected_citations"] == [FAKE_ID]
    assert "status" not in h1  # only Verification sets status (T4)

    # Evidence: planner 3 + telemetry 3 per focus service + deploys + dependency 2.
    kinds = Counter(r["kind"] for r in await evidence_rows(sessionmaker, gw["inv"]))
    assert kinds["log_query"] == 2
    assert kinds["metric_query"] == 2
    assert kinds["detection_ref"] == 1

    # Usage rows: one per call, fake provider, linked to the investigation (no text).
    calls = await rows(
        sessionmaker,
        "SELECT node, provider, outcome FROM llm_calls WHERE investigation_id = :i ORDER BY id",
        i=gw["inv"],
    )
    assert Counter(c["node"] for c in calls) == Counter(
        {"planner": 1, "telemetry": 1, "dependency": 1, "analyst": 1}
    )
    assert {c["provider"] for c in calls} == {"fake"}

    (row,) = await rows(
        sessionmaker,
        "SELECT status, incomplete_reasons, lease_owner, finished_at FROM investigations"
        " WHERE id = :i",
        i=gw["inv"],
    )
    assert row["status"] == "incomplete"
    assert row["lease_owner"] is None
    assert row["finished_at"] is not None
    assert row["incomplete_reasons"] == out.reasons

    # Checkpoint state holds ids and summaries, not raw telemetry.
    snap = await checkpointer.aget({"configurable": {"thread_id": str(gw["inv"])}})
    assert snap is not None
    blob = json.dumps(snap["channel_values"], default=str)
    assert "waiting for a connection" not in blob
    assert "slow query took" not in blob

    # Untrusted data reached the model only inside blocks.
    for req in provider.requests:
        for block in req.user_blocks:
            if "connection pool exhausted" in block:
                outside = re.sub(
                    r"<untrusted_telemetry .*?</untrusted_telemetry>", "", block, flags=re.S
                )
                assert "connection pool exhausted" not in outside


async def test_investigators_run_concurrently(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    """Each branch waits for the other to have started: sequential execution would time out."""
    started = {"telemetry": asyncio.Event(), "dependency": asyncio.Event()}

    def rendezvous(me: str, other: str) -> Callable[[], Any]:
        async def hook() -> None:
            started[me].set()
            await asyncio.wait_for(started[other].wait(), timeout=10)

        return hook

    out = await run(
        sessionmaker,
        gw,
        checkpointer,
        scripted(),
        hooks={
            "telemetry.start": rendezvous("telemetry", "dependency"),
            "dependency.start": rendezvous("dependency", "telemetry"),
        },
    )
    assert out.status == "incomplete"
    assert all(e.is_set() for e in started.values())


async def test_crash_in_analyst_resumes_from_checkpoint(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    crashes = {"n": 0}

    def analyst_crashes_once(req: ProviderRequest) -> FakeStep:
        if crashes["n"] == 0:
            crashes["n"] += 1
            raise CrashError("worker died during the analyst call")
        return scripted().responder(req)  # type: ignore[misc]

    provider = scripted({"analyst": analyst_crashes_once})
    with pytest.raises(CrashError):
        await run(sessionmaker, gw, checkpointer, provider)
    (row,) = await rows(
        sessionmaker, "SELECT status, lease_owner FROM investigations WHERE id = :i", i=gw["inv"]
    )
    assert row == {"status": "running", "lease_owner": None}  # resumable at once
    before = await evidence_rows(sessionmaker, gw["inv"])
    asked_before = Counter(prompt_of(r) for r in provider.requests)

    out = await run(sessionmaker, gw, checkpointer, provider, owner="worker-2")
    assert out.resumed is True
    assert out.status == "incomplete"
    assert len(out.state["hypotheses"]) == 2
    asked_after = Counter(prompt_of(r) for r in provider.requests) - asked_before
    assert asked_after == Counter({"analyst": 1})  # planner/investigators were not re-run
    assert await evidence_rows(sessionmaker, gw["inv"]) == before


async def test_crash_mid_branch_reruns_only_that_node_without_duplicate_evidence(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    state = {"crashed": False}
    dependency_done = asyncio.Event()

    async def die_once() -> None:
        if not state["crashed"]:
            state["crashed"] = True
            # Let the sibling branch finish first, so the test shows its writes are kept.
            await asyncio.wait_for(dependency_done.wait(), timeout=10)
            raise CrashError("worker died after the telemetry investigator's first tool")

    async def mark_done() -> None:
        dependency_done.set()

    provider = scripted()
    with pytest.raises(CrashError):
        await run(
            sessionmaker,
            gw,
            checkpointer,
            provider,
            hooks={"telemetry.after_first_tool": die_once, "dependency.end": mark_done},
        )
    partial = await evidence_rows(sessionmaker, gw["inv"])
    assert any(r["kind"] == "log_query" for r in partial)  # committed before the crash
    asked_before = Counter(prompt_of(r) for r in provider.requests)

    out = await run(
        sessionmaker,
        gw,
        checkpointer,
        provider,
        owner="worker-2",
        hooks={"telemetry.after_first_tool": die_once, "dependency.end": mark_done},
    )
    assert out.resumed is True
    assert out.status == "incomplete"
    asked_after = Counter(prompt_of(r) for r in provider.requests) - asked_before
    # The dependency branch finished in the failed superstep: its writes were kept.
    assert asked_after == Counter({"telemetry": 1, "analyst": 1})
    resumed_rows = await evidence_rows(sessionmaker, gw["inv"])
    assert len({r["dedup_key"] for r in resumed_rows}) == len(resumed_rows)

    # Same evidence as an uninterrupted run of the same incident.
    await sql(
        sessionmaker, "UPDATE investigations SET status = 'completed' WHERE id = :i", i=gw["inv"]
    )
    (fresh,) = await rows(
        sessionmaker,
        "INSERT INTO investigations (org_id, incident_id, status) VALUES (:o, :i, 'queued')"
        " RETURNING id",
        o=gw["org"],
        i=gw["incident"],
    )
    clean = await run(sessionmaker, gw, checkpointer, scripted(), inv=fresh["id"])
    assert clean.resumed is False
    clean_rows = await evidence_rows(sessionmaker, fresh["id"])
    assert sorted(r["dedup_key"] for r in clean_rows) == sorted(
        r["dedup_key"] for r in resumed_rows
    )


async def test_lease_held_elsewhere_skips(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    from datetime import timedelta

    async with sessionmaker() as s:
        repo = InvestigationRepository(s, SystemScope(gw["org"], "t"))
        assert await repo.try_lease(gw["inv"], "other-worker", timedelta(seconds=60)) is not None
        await s.commit()
    provider = scripted()
    out = await run(sessionmaker, gw, checkpointer, provider)
    assert out.status == "skipped"
    assert provider.requests == []
    # Expired lease: taken over.
    await sql(
        sessionmaker,
        "UPDATE investigations SET lease_expires_at = now() - interval '1 second' WHERE id = :i",
        i=gw["inv"],
    )
    assert (await run(sessionmaker, gw, checkpointer, provider)).status == "incomplete"
    # Finished: a redelivered task does nothing.
    assert (await run(sessionmaker, gw, checkpointer, provider, owner="late")).skipped


async def test_planner_failure_falls_back_deterministically(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    provider = scripted({"planner": lambda req: Respond("not json")})
    out = await run(sessionmaker, gw, checkpointer, provider)
    assert out.state["plan_source"] == "fallback:llm_invalid_output:planner"
    assert set(out.state["focus"]) == {str(gw["gw"]), str(gw["orders"])}
    assert out.state["stages"][-1] == "report"


async def test_analyst_failure_is_incomplete_not_a_guess(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    provider = scripted({"analyst": lambda req: Respond({"hypotheses": "nope"})})
    out = await run(sessionmaker, gw, checkpointer, provider)
    assert out.state["hypotheses"] == []
    assert "llm_invalid_output:analyst" in out.reasons
    assert out.status == "incomplete"


async def test_foreign_org_cannot_run_the_investigation(
    client: Any, sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    from tests.integration.factories import register

    other = await register(client, "Other", "Other Org")
    provider = scripted()
    out = await run_investigation(
        org_id=uuid.UUID(other.org_id),
        investigation_id=gw["inv"],
        sessionmaker=sessionmaker,
        llm=llm(sessionmaker, uuid.UUID(other.org_id), provider),
        checkpointer=checkpointer,
        owner="w",
    )
    assert out.skipped == "not_leased_or_not_active"
    assert provider.requests == []
