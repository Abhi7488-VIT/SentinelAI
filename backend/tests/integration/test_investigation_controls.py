"""Controls (Phase 4 T5): budgets, cancellation, dead-letter, sweep, SIGKILL resume."""

# ruff: noqa: F401, F811  (pytest fixtures imported from sibling modules, used as arguments)

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.tenancy import SystemScope
from app.investigation.runner import dead_letter, run_investigation
from app.llm.providers.fake import FakeProvider
from app.repositories.investigations import InvestigationRepository, stuck_investigations
from tests.integration.telemetry_support import SM, rows, sql
from tests.integration.test_investigation_graph import (
    checkpointer,
    gw,
    llm,
    prompt_of,
    run,
    scripted,
)
from tests.integration.test_investigation_tools import partitions, t0, world

BACKEND = Path(__file__).resolve().parents[2]


async def hypotheses(sm: SM, inv: Any) -> list[dict[str, Any]]:
    return await rows(
        sm,
        "SELECT category, status, rank FROM hypotheses WHERE investigation_id = :i ORDER BY rank",
        i=inv,
    )


async def row(sm: SM, inv: Any) -> dict[str, Any]:
    (r,) = await rows(sm, "SELECT * FROM investigations WHERE id = :i", i=inv)
    return r


@pytest.mark.parametrize(
    ("setup", "budget"),
    [
        ("budget_steps = 1", "steps"),
        ("budget_tool_calls = 2", "tool_calls"),
        ("budget_llm_calls = 1", "llm_calls"),
        ("budget_tokens = 1", "tokens"),
        ("budget_cost_usd = 0.0001", "cost"),
        ("started_at = now() - interval '1 hour'", "wall_clock"),
    ],
)
async def test_each_budget_stops_the_run_incomplete(
    sessionmaker: SM,
    gw: dict[str, Any],
    checkpointer: AsyncPostgresSaver,
    setup: str,
    budget: str,
) -> None:
    await sql(sessionmaker, f"UPDATE investigations SET {setup} WHERE id = :i", i=gw["inv"])  # noqa: S608
    # Priced like the real model so cost is computed (the text is still fake).
    provider = scripted()
    provider.model = "claude-sonnet-5-5"
    out = await run(sessionmaker, gw, checkpointer, provider)

    assert out.status == "incomplete"
    assert out.reasons[-1] == f"budget:{budget}"
    assert await hypotheses(sessionmaker, gw["inv"]) == []  # nothing verified, nothing guessed
    r = await row(sessionmaker, gw["inv"])
    assert r["status"] == "incomplete"
    assert r["incomplete_reasons"] == out.reasons
    assert r["lease_owner"] is None
    calls = await rows(
        sessionmaker, "SELECT count(*) AS n FROM llm_calls WHERE investigation_id = :i", i=gw["inv"]
    )
    if budget == "llm_calls":
        assert calls[0]["n"] == 1  # the refused call was not made
    if budget == "steps":
        assert r["used_steps"] == 1
    if budget == "tool_calls":
        assert r["used_tool_calls"] == 2


async def test_budget_after_verification_keeps_what_was_verified(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    await sql(sessionmaker, "UPDATE investigations SET budget_steps = 5 WHERE id = :i", i=gw["inv"])
    out = await run(sessionmaker, gw, checkpointer, scripted())
    assert out.reasons[-1] == "budget:steps"  # stopped before the report node
    assert len(await hypotheses(sessionmaker, gw["inv"])) == 2


async def test_cancel_mid_node_stops_within_seconds(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    scope = SystemScope(gw["org"], "t")
    asked: dict[str, float] = {}

    async def cancel_then_work_long() -> None:
        async with sessionmaker() as s:
            assert await InvestigationRepository(s, scope).request_cancel(gw["inv"])
            await s.commit()
        asked["at"] = time.monotonic()
        await asyncio.sleep(60)  # a long tool loop / LLM call in flight

    provider = scripted()
    out = await run_investigation(
        org_id=gw["org"],
        investigation_id=gw["inv"],
        sessionmaker=sessionmaker,
        llm=llm(sessionmaker, gw["org"], provider),
        checkpointer=checkpointer,
        owner="w",
        hooks={"telemetry.after_first_tool": cancel_then_work_long},
        poll_every_s=0.2,
    )
    stopped_after = time.monotonic() - asked["at"]
    assert out.status == "cancelled"
    assert stopped_after < 5.0, stopped_after
    asked_prompts = Counter(prompt_of(r) for r in provider.requests)
    assert asked_prompts["analyst"] == 0  # nothing ran after the cancel
    r = await row(sessionmaker, gw["inv"])
    assert (r["status"], r["lease_owner"]) == ("cancelled", None)
    assert r["incomplete_reasons"][-1] == "cancelled"
    assert await hypotheses(sessionmaker, gw["inv"]) == []


async def test_cancel_between_nodes_and_before_start(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    scope = SystemScope(gw["org"], "t")
    # Requested while queued: never runs.
    await sql(
        sessionmaker, "UPDATE investigations SET status = 'queued' WHERE id = :i", i=gw["inv"]
    )
    async with sessionmaker() as s:
        assert await InvestigationRepository(s, scope).request_cancel(gw["inv"])
        await s.commit()
    provider = scripted()
    out = await run(sessionmaker, gw, checkpointer, provider)
    assert out.status == "cancelled"
    assert provider.requests == []
    assert (await row(sessionmaker, gw["inv"]))["status"] == "cancelled"
    async with sessionmaker() as s:  # cancelling a finished run is refused
        assert not await InvestigationRepository(s, scope).request_cancel(gw["inv"])


async def test_dead_letter_is_recorded_once_and_fails_the_run(
    sessionmaker: SM, gw: dict[str, Any]
) -> None:
    scope = SystemScope(gw["org"], "investigation")
    await sql(
        sessionmaker, "UPDATE investigations SET last_node = 'analyst' WHERE id = :i", i=gw["inv"]
    )
    for _ in range(2):
        await dead_letter(sessionmaker, scope, gw["inv"], "OperationalError", 4)
    letters = await rows(
        sessionmaker,
        "SELECT error_class, attempts, last_node FROM investigation_dead_letters"
        " WHERE investigation_id = :i",
        i=gw["inv"],
    )
    assert letters == [{"error_class": "OperationalError", "attempts": 4, "last_node": "analyst"}]
    r = await row(sessionmaker, gw["inv"])
    assert (r["status"], r["incomplete_reasons"]) == ("failed", ["failed:OperationalError"])


async def test_a_run_that_keeps_dying_is_dead_lettered(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver
) -> None:
    await sql(sessionmaker, "UPDATE investigations SET attempts = 5 WHERE id = :i", i=gw["inv"])
    provider = scripted()
    out = await run(sessionmaker, gw, checkpointer, provider)
    assert (out.status, out.reasons) == ("failed", ["too_many_attempts"])
    assert provider.requests == []
    (letter,) = await rows(
        sessionmaker,
        "SELECT error_class FROM investigation_dead_letters WHERE investigation_id = :i",
        i=gw["inv"],
    )
    assert letter["error_class"] == "too_many_attempts"


async def test_sweep_finds_only_runs_whose_worker_is_gone(
    sessionmaker: SM, gw: dict[str, Any]
) -> None:
    async def stuck() -> set[Any]:
        async with sessionmaker() as s:
            found = await stuck_investigations(s, stale=timedelta(minutes=2), limit=10)
        return {i for _, i in found}

    inv = gw["inv"]
    await sql(
        sessionmaker,
        "UPDATE investigations SET lease_owner = 'w',"
        " lease_expires_at = now() + interval '1 minute' WHERE id = :i",
        i=inv,
    )
    assert inv not in await stuck()  # live lease
    await sql(
        sessionmaker,
        "UPDATE investigations SET lease_expires_at = now() - interval '1 second' WHERE id = :i",
        i=inv,
    )
    assert inv in await stuck()  # expired lease (worker killed)
    await sql(
        sessionmaker,
        "UPDATE investigations SET lease_owner = NULL, lease_expires_at = NULL,"
        " updated_at = now() WHERE id = :i",
        i=inv,
    )
    assert inv not in await stuck()  # released just now: its task retry is pending
    await sql(
        sessionmaker,
        "UPDATE investigations SET updated_at = now() - interval '5 minutes' WHERE id = :i",
        i=inv,
    )
    assert inv in await stuck()  # released long ago: the retry never came
    await sql(sessionmaker, "UPDATE investigations SET status = 'completed' WHERE id = :i", i=inv)
    assert inv not in await stuck()


async def test_sigkill_mid_node_then_resume_without_duplicate_evidence(
    sessionmaker: SM, gw: dict[str, Any], checkpointer: AsyncPostgresSaver, tmp_path: Path
) -> None:
    """A real worker process is SIGKILLed inside the telemetry investigator (after its first
    tool). Its lease expires; the sweep finds the run; a new worker resumes from the checkpoint:
    the planner is not re-run and the evidence equals an uninterrupted run's."""
    marker = tmp_path / "marker"
    child = subprocess.Popen(  # noqa: S603, ASYNC220 (a real OS process is the point)
        [
            sys.executable,
            "-m",
            "tests.integration.sigkill_worker",
            str(gw["org"]),
            str(gw["inv"]),
            str(marker),
        ],
        cwd=BACKEND,
        env={**os.environ},
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert child.poll() is None, "worker exited early"
            assert time.monotonic() < deadline, "worker never reached the first tool"
            await asyncio.sleep(0.1)
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()

    r = await row(sessionmaker, gw["inv"])
    assert (r["status"], r["lease_owner"]) == ("running", "doomed-worker")
    killed_evidence = await rows(
        sessionmaker, "SELECT kind FROM evidence WHERE investigation_id = :i", i=gw["inv"]
    )
    assert "log_query" in {e["kind"] for e in killed_evidence}

    # No redelivery in this test: the lease runs out and the sweep picks the run up.
    await sql(
        sessionmaker,
        "UPDATE investigations SET lease_expires_at = now() - interval '1 second' WHERE id = :i",
        i=gw["inv"],
    )
    async with sessionmaker() as s:
        assert gw["inv"] in {
            i for _, i in await stuck_investigations(s, stale=timedelta(minutes=2), limit=10)
        }

    provider = scripted()
    out = await run(sessionmaker, gw, checkpointer, provider, owner="worker-2")
    assert out.resumed is True
    assert out.status == "incomplete"
    asked = Counter(prompt_of(r) for r in provider.requests)
    assert asked["planner"] == 0  # resumed from the checkpoint after the planner
    assert asked["telemetry"] == 1
    assert asked["analyst"] == 1
    resumed_keys = sorted(
        x["dedup_key"]
        for x in await rows(
            sessionmaker, "SELECT dedup_key FROM evidence WHERE investigation_id = :i", i=gw["inv"]
        )
    )
    assert len(resumed_keys) == len(set(resumed_keys))
    assert (await row(sessionmaker, gw["inv"]))["attempts"] == 2

    # Same evidence as an uninterrupted run.
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
    await run(sessionmaker, gw, checkpointer, scripted(), inv=fresh["id"])
    clean_keys = sorted(
        x["dedup_key"]
        for x in await rows(
            sessionmaker,
            "SELECT dedup_key FROM evidence WHERE investigation_id = :i",
            i=fresh["id"],
        )
    )
    assert resumed_keys == clean_keys
