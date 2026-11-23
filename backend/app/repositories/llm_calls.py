"""`llm_calls` access, tenant-scoped. Also the database `UsageSink` for `LLMClient`."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func

from app.llm.types import CallRecord
from app.models.llm_call import LLMCall
from app.repositories.base import TenantIsolationError, TenantRepository


class LLMCallRepository(TenantRepository[LLMCall]):
    model = LLMCall

    async def record(self, record: CallRecord) -> None:
        """`UsageSink`: one row per attempt. Rejects records for another org."""
        if record.org_id != self.ctx.org_id:
            raise TenantIsolationError("LLM call record for another org")
        self.session.add(
            LLMCall(
                org_id=self.ctx.org_id,
                investigation_id=record.investigation_id,
                node=record.node[:32],
                purpose=record.purpose,
                provider=record.provider,
                model=record.model[:64],
                prompt_id=record.prompt_id,
                prompt_version=record.prompt_version,
                prompt_sha256=record.prompt_sha256,
                attempt=record.attempt,
                outcome=record.outcome,
                error_class=record.error_class,
                input_tokens=record.usage.input_tokens,
                output_tokens=record.usage.output_tokens,
                cache_read_tokens=record.usage.cache_read_tokens,
                cache_write_tokens=record.usage.cache_write_tokens,
                latency_ms=record.latency_ms,
                cost_usd=record.cost_usd,
                provider_request_id=record.provider_request_id,
            )
        )
        await self.session.flush()

    async def for_investigation(self, investigation_id: uuid.UUID) -> Sequence[LLMCall]:
        stmt = (
            self.scoped().where(LLMCall.investigation_id == investigation_id).order_by(LLMCall.id)
        )
        return (await self.session.scalars(stmt)).all()

    async def totals(self, investigation_id: uuid.UUID) -> tuple[int, int, Decimal]:
        """(attempts, tokens in+out+cache, cost) of this investigation's LLM calls so far."""
        stmt = (
            self.scoped()
            .where(LLMCall.investigation_id == investigation_id)
            .with_only_columns(
                func.count(),
                func.coalesce(
                    func.sum(
                        LLMCall.input_tokens
                        + LLMCall.output_tokens
                        + LLMCall.cache_read_tokens
                        + LLMCall.cache_write_tokens
                    ),
                    0,
                ),
                func.coalesce(func.sum(LLMCall.cost_usd), 0),
            )
        )
        n, tokens, cost = (await self.session.execute(stmt)).one()
        return int(n), int(tokens), Decimal(cost)
