"""`llm_calls` persistence through the real client (FakeProvider) and tenant scoping (T1)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import SystemScope
from app.llm.client import LLMClient
from app.llm.errors import LLMInvalidOutputError
from app.llm.pricing import DEFAULT_PRICES
from app.llm.providers.fake import FakeProvider, Respond
from app.llm.types import CallContext, LLMRequest
from app.repositories.base import TenantIsolationError
from app.repositories.llm_calls import LLMCallRepository
from tests.integration.factories import register
from tests.integration.investigation_support import create_investigation
from tests.unit.test_llm_client import GOOD, SECRET_TEXT, Finding

pytestmark = pytest.mark.integration


def _client(provider: FakeProvider, sink: LLMCallRepository) -> LLMClient:
    return LLMClient(
        provider,
        sink,
        DEFAULT_PRICES,
        timeout_s=5,
        max_invalid_retries=1,
        max_transport_retries=0,
        max_input_tokens=30_000,
    )


def _request() -> LLMRequest[Finding]:
    return LLMRequest(
        purpose="analyst",
        prompt_id="t1",
        prompt_version=1,
        system="system",
        user_blocks=(SECRET_TEXT,),
        output_model=Finding,
    )


async def test_every_attempt_is_stored_with_usage_and_no_text(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    org = uuid.UUID((await register(client, "Llm")).org_id)
    investigation = await create_investigation(session, org)
    repo = LLMCallRepository(session, SystemScope(org, "test"))
    # Priced as the real model so cost is computed and stored (the fake text is still fake).
    provider = FakeProvider([Respond("not json"), Respond(GOOD)], model="claude-sonnet-5-5")
    result = await _client(provider, repo).generate(
        _request(), CallContext(org_id=org, investigation_id=investigation, node="analyst")
    )
    await session.commit()

    rows = await repo.for_investigation(investigation)
    assert [r.outcome for r in rows] == ["invalid_output", "ok"]
    assert [r.attempt for r in rows] == [1, 2]
    assert all(r.provider == "fake" and r.org_id == org for r in rows)
    assert sum(r.input_tokens + r.output_tokens for r in rows) == result.usage.total_tokens
    assert sum(r.cost_usd or 0 for r in rows) == result.cost_usd
    for r in rows:
        stored = " ".join(str(v) for v in r.__dict__.values())
        assert SECRET_TEXT not in stored
        assert "pool saturated" not in stored


async def test_failed_call_rows_survive(client: httpx.AsyncClient, session: AsyncSession) -> None:
    org = uuid.UUID((await register(client, "LlmFail")).org_id)
    investigation = await create_investigation(session, org)
    repo = LLMCallRepository(session, SystemScope(org, "test"))
    with pytest.raises(LLMInvalidOutputError):
        await _client(FakeProvider([Respond("x"), Respond("y")]), repo).generate(
            _request(), CallContext(org_id=org, investigation_id=investigation)
        )
    await session.commit()
    rows = await repo.for_investigation(investigation)
    assert [r.outcome for r in rows] == ["invalid_output", "invalid_output"]


async def test_records_are_tenant_scoped(client: httpx.AsyncClient, session: AsyncSession) -> None:
    org_a = uuid.UUID((await register(client, "LlmA")).org_id)
    org_b = uuid.UUID((await register(client, "LlmB")).org_id)
    investigation = await create_investigation(session, org_a)
    repo_a = LLMCallRepository(session, SystemScope(org_a, "test"))
    repo_b = LLMCallRepository(session, SystemScope(org_b, "test"))

    # A record for org A cannot be written through org B's repository.
    with pytest.raises(TenantIsolationError):
        await _client(FakeProvider([Respond(GOOD)]), repo_b).generate(
            _request(), CallContext(org_id=org_a, investigation_id=investigation)
        )
    await _client(FakeProvider([Respond(GOOD)]), repo_a).generate(
        _request(), CallContext(org_id=org_a, investigation_id=investigation)
    )
    await session.commit()

    assert len(await repo_a.for_investigation(investigation)) == 1
    assert await repo_b.for_investigation(investigation) == []
