"""Events, SSE and report (Phase 4 T7) on seeded telemetry with the FakeProvider."""

# ruff: noqa: F401, F811  (pytest fixtures imported from sibling modules, used as arguments)

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.investigation.report import SECTIONS, cited_ids
from app.llm.providers.fake import FakeStep, Respond
from app.llm.types import ProviderRequest
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
from tests.integration.test_investigation_verification import analyst_blaming
from tests.unit.test_llm_client import SECRET_TEXT


async def events_of(sm: SM, inv: Any) -> list[dict[str, Any]]:
    return await rows(
        sm,
        "SELECT id, type, payload FROM investigation_events WHERE investigation_id = :i"
        " ORDER BY id",
        i=inv,
    )


def parse_sse(body: str) -> list[dict[str, Any]]:
    out = []
    for frame in body.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in frame.splitlines() if line and not line.startswith(":")
        )
        if fields:
            out.append(
                {
                    "id": int(fields["id"]),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return out


async def test_events_tell_the_run_in_order_without_text_or_secrets(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    await sql(
        sessionmaker, "UPDATE investigations SET budget_llm_calls = 5 WHERE id = :i", i=gw["inv"]
    )
    provider = scripted({"analyst": analyst_blaming(str(gw["orders"]))})
    await run(sessionmaker, gw, checkpointer, provider)
    evs = await events_of(sessionmaker, gw["inv"])
    types = [e["type"] for e in evs]

    assert [e["id"] for e in evs] == sorted(e["id"] for e in evs)  # monotonic
    assert evs[0]["type"] == "status_changed"
    assert evs[0]["payload"]["status"] == "running"
    assert evs[-1]["type"] == "status_changed"
    assert evs[-1]["payload"]["status"] == "incomplete"
    assert types.index("report_ready") < len(types) - 1
    starts = [e["payload"]["stage"] for e in evs if e["type"] == "stage_started"]
    assert starts[0] == "planner"
    assert starts[-3:] == ["analyst", "verification", "report"]
    assert types.count("hypothesis_added") == 2
    assert types.count("hypothesis_verified") == 2
    assert types.count("llm_call") == 4
    # One warning per budget: 4 of 5 LLM calls reached the 80 % mark.
    warnings = [e["payload"] for e in evs if e["type"] == "budget_warning"]
    assert [w["budget"] for w in warnings].count("llm_calls") == 1
    rejected = [e["payload"] for e in evs if e["type"] == "rejected_by_allowlist"]
    assert {"node": "analyst", "kind": "citation", "value": FAKE_ID} in rejected

    stored = {
        r["id"]
        for r in await rows(
            sessionmaker, "SELECT id FROM evidence WHERE investigation_id = :i", i=gw["inv"]
        )
    }
    tool_events = [e["payload"] for e in evs if e["type"] == "tool_completed"]
    assert tool_events
    assert {t["evidence_id"] for t in tool_events} <= stored

    for e in evs:  # no prompt text, no credentials, bounded size
        blob = json.dumps(e["payload"])
        assert len(blob.encode()) <= 4096
        assert "SentinelAI prompt" not in blob
        assert SECRET_TEXT not in blob
        assert "waiting for a connection" not in blob or e["type"] == "tool_completed"
        assert not any(k in e["payload"] for k in ("prompt", "text", "authorization", "api_key"))
        if "summary" in e["payload"]:
            assert len(e["payload"]["summary"]) <= 1000


async def test_report_has_14_sections_and_only_resolving_citations(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    out = await run(
        sessionmaker, gw, checkpointer, scripted({"analyst": analyst_blaming(str(gw["orders"]))})
    )
    (row,) = await rows(
        sessionmaker,
        "SELECT report, cited_evidence_ids FROM investigation_reports WHERE investigation_id = :i",
        i=gw["inv"],
    )
    report = row["report"]
    assert report["sections"] == list(SECTIONS)
    assert len(SECTIONS) == 14
    assert all(k in report for k in SECTIONS)
    stored = {
        r["id"]
        for r in await rows(
            sessionmaker, "SELECT id FROM evidence WHERE investigation_id = :i", i=gw["inv"]
        )
    }
    assert set(cited_ids(report)) <= stored
    assert row["cited_evidence_ids"] == cited_ids(report)

    primary = report["primary_candidate"]
    assert (primary["category"], primary["status"]) == ("resource_exhaustion", "confirmed")
    assert primary["subjects"] == ["orders"]
    for ev_id in primary["supporting_evidence_ids"]:
        assert ev_id in report["summary"]["text"]  # the summary cites its evidence
    assert report["summary"]["text"].startswith(
        "Primary candidate (confirmed): resource_exhaustion in orders."
    )
    assert report["status"]["incomplete_reasons"] == out.reasons
    assert report["gaps"]["rejected_citations"] == 2  # counted, not listed (they do not resolve)
    meta = report["run_metadata"]
    assert meta["usage_label"] == "fake provider counts"
    assert meta["cost_usd"] is None
    assert meta["llm_calls"] == 4
    assert meta["prompt_versions"] == ["analyst.v1", "dependency.v1", "planner.v1", "telemetry.v1"]
    assert report["next_steps"]["suggestions_only"] is True
    assert any("db server metrics" in i for i in report["next_steps"]["items"])
    assert any(t["what"].startswith("deployment of orders") for t in report["timeline"])
    assert any(i["service"] == "orders" for i in report["impact"])


async def test_report_with_an_unresolved_id_is_not_stored(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    """The model writes a made-up id into its prose: the citation gate refuses the report."""
    orders = str(gw["orders"])

    def respond(req: ProviderRequest) -> FakeStep:
        ids = EV.findall("\n".join(req.user_blocks))
        h = {
            "category": "resource_exhaustion",
            "subject_service_ids": [orders],
            "statement": f"As shown by {FAKE_ID}, the pool ran out.",
            "supporting_evidence_ids": ids,
            "contradicting_evidence_ids": [],
            "missing_evidence": [],
            "confidence": 0.5,
        }
        return Respond({"hypotheses": [h, {**h, "category": "other"}]})

    out = await run(sessionmaker, gw, checkpointer, scripted({"analyst": respond}))
    assert "report_citation_check_failed" in out.reasons
    assert out.status == "incomplete"
    assert (
        await rows(
            sessionmaker,
            "SELECT id FROM investigation_reports WHERE investigation_id = :i",
            i=gw["inv"],
        )
        == []
    )
    assert "report_ready" not in [e["type"] for e in await events_of(sessionmaker, gw["inv"])]


async def test_sse_replays_resumes_and_closes_when_terminal(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    await run(sessionmaker, gw, checkpointer, scripted())
    db_events = await events_of(sessionmaker, gw["inv"])
    url = f"/api/v1/orgs/{gw['org']}/investigations/{gw['inv']}/events"
    headers = gw["owner"].headers

    resp = await client.get(url, headers=headers)  # returns: the stream ended (terminal)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(resp.text)
    assert [f["id"] for f in frames] == [e["id"] for e in db_events]
    assert [f["event"] for f in frames] == [e["type"] for e in db_events]
    assert frames[-1]["data"]["status"] == "incomplete"

    cut = db_events[4]["id"]
    resumed = parse_sse(
        (await client.get(url, headers={**headers, "Last-Event-ID": str(cut)})).text
    )
    assert [f["id"] for f in resumed] == [e["id"] for e in db_events if e["id"] > cut]
    by_query = parse_sse((await client.get(f"{url}?after={cut}", headers=headers)).text)
    assert by_query == resumed
    assert (
        parse_sse(
            (
                await client.get(
                    url, headers={**headers, "Last-Event-ID": str(db_events[-1]["id"])}
                )
            ).text
        )
        == []
    )

    bad = await client.get(url, headers={**headers, "Last-Event-ID": "abc"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_last_event_id"


async def test_sse_delivers_events_of_a_live_run(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    url = f"/api/v1/orgs/{gw['org']}/investigations/{gw['inv']}/events"
    runner = asyncio.create_task(run(sessionmaker, gw, checkpointer, scripted()))
    resp = await asyncio.wait_for(client.get(url, headers=gw["owner"].headers), timeout=60)
    await runner
    frames = parse_sse(resp.text)
    assert [f["id"] for f in frames] == [e["id"] for e in await events_of(sessionmaker, gw["inv"])]
    assert frames[-1]["event"] == "status_changed"


async def test_sse_is_tenant_scoped(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
) -> None:
    await run(sessionmaker, gw, checkpointer, scripted())
    other = await register(client, "Other", "Other Org")
    foreign = await client.get(
        f"/api/v1/orgs/{gw['org']}/investigations/{gw['inv']}/events", headers=other.headers
    )
    assert foreign.status_code == 404  # not a member of this org
    # Their own org, this org's investigation id: not found either.
    cross = await client.get(
        f"/api/v1/orgs/{other.org_id}/investigations/{gw['inv']}/events", headers=other.headers
    )
    assert cross.status_code == 404
    assert cross.json()["error"]["code"] == "investigation_not_found"
    missing = await client.get(
        f"/api/v1/orgs/{gw['org']}/investigations/{uuid.uuid4()}/events",
        headers=gw["owner"].headers,
    )
    assert missing.status_code == 404
    assert (
        await client.get(f"/api/v1/orgs/{gw['org']}/investigations/{gw['inv']}/events")
    ).status_code == 401
