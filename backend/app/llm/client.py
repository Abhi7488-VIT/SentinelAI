"""The single entry point for LLM calls (CLAUDE.md: one provider interface).

Per call:
- the prompt size is estimated and refused above `max_input_tokens` (nothing is sent);
- each attempt runs under `asyncio.timeout`, after the caller's `pre_call` budget hook;
- transport failures (timeout, 429, 5xx, connection) are retried with exponential backoff and
  full jitter, at most `max_transport_retries` times; non-retryable errors are not retried;
- the output is validated against the request's Pydantic model; a failure, a `max_tokens` stop
  or a schema violation is retried at most `max_invalid_retries` times with a fixed-format note
  (error locations and types only, never the model's text), then `LLMInvalidOutputError` is raised;
- a refusal is never retried here (server-side fallback already ran) → `LLMRefusalError`;
- every attempt, failed or not, is recorded (tokens, latency, cost, outcome, error class) — never
  the prompt or the output text.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from app.core.metrics import LLM_CALLS, LLM_LATENCY, LLM_TOKENS
from app.llm.errors import (
    LLMFatalError,
    LLMInputTooLargeError,
    LLMInvalidOutputError,
    LLMNotConfiguredError,
    LLMRefusalError,
    LLMTimeoutError,
    LLMTransportError,
    ProviderError,
)
from app.llm.pricing import ModelPrice, cost_usd
from app.llm.schema import provider_schema
from app.llm.types import (
    CallContext,
    CallRecord,
    LLMRequest,
    LLMResult,
    Outcome,
    Provider,
    ProviderRequest,
    RawCompletion,
    Usage,
    UsageSink,
)

# Conservative estimate (≈ 4 characters per token for English/JSON). Used only to refuse
# oversized prompts before sending; billed counts always come from the provider's usage.
CHARS_PER_TOKEN = 4

INVALID_OUTPUT_NOTE = (
    "Operator note: your previous response did not match the required JSON schema. "
    "Problems (location: type): {problems}. Respond again with JSON only, matching the schema."
)


def estimate_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // CHARS_PER_TOKEN + 1


def prompt_sha256[T: BaseModel](request: LLMRequest[T]) -> str:
    h = hashlib.sha256()
    for part in (
        request.prompt_id,
        str(request.prompt_version),
        request.system,
        *request.user_blocks,
    ):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


def validation_note(error: ValidationError) -> str:
    # loc + type only: Pydantic's `msg`/`input` can echo the model's own text back.
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}"
        for e in error.errors()[:20]
    )
    return INVALID_OUTPUT_NOTE.format(problems=problems)


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        sink: UsageSink,
        prices: dict[str, ModelPrice],
        *,
        timeout_s: float,
        max_invalid_retries: int,
        max_transport_retries: int,
        max_input_tokens: int,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self._sink = sink
        self._price = prices.get(provider.model)
        if self._price is None and provider.name != "fake":
            raise LLMNotConfiguredError(f"no price configured for model {provider.model!r}")
        self._timeout_s = timeout_s
        self._max_invalid = max_invalid_retries
        self._max_transport = max_transport_retries
        self._max_input_tokens = max_input_tokens
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._sleep = sleep
        self._rand = rand

    @property
    def is_fake(self) -> bool:
        return self.provider.name == "fake"

    async def generate[T: BaseModel](
        self, request: LLMRequest[T], ctx: CallContext
    ) -> LLMResult[T]:
        estimated = estimate_tokens(request.system, *request.user_blocks)
        if estimated > self._max_input_tokens:
            raise LLMInputTooLargeError(estimated, self._max_input_tokens)

        schema = provider_schema(request.output_model)
        sha = prompt_sha256(request)
        state = _CallState()
        note: str | None = None

        for _ in range(1 + self._max_invalid):
            blocks = request.user_blocks + ((note,) if note else ())
            preq = ProviderRequest(
                model=self.provider.model,
                system=request.system,
                user_blocks=blocks,
                json_schema=schema,
                max_output_tokens=request.max_output_tokens,
            )
            raw = await self._attempt_with_transport_retries(preq, request, ctx, sha, state)

            if raw.stop_reason == "refusal":
                await self._record(request, ctx, sha, state, "refusal", "refusal", raw)
                raise LLMRefusalError("the model declined the request")
            if raw.stop_reason == "max_tokens":
                await self._record(request, ctx, sha, state, "invalid_output", "max_tokens", raw)
                note = INVALID_OUTPUT_NOTE.format(problems="<root>: truncated_at_max_tokens")
                continue
            try:
                parsed = request.output_model.model_validate_json(raw.text)
            except ValidationError as exc:
                await self._record(
                    request, ctx, sha, state, "invalid_output", "ValidationError", raw
                )
                note = validation_note(exc)
                continue

            await self._record(request, ctx, sha, state, "ok", None, raw)
            return LLMResult(
                parsed=parsed,
                usage=state.usage,
                cost_usd=state.cost,
                latency_ms=state.latency_ms,
                model=raw.model,
                provider=self.provider.name,
                attempts=state.attempts,
                provider_request_id=raw.request_id,
                records=state.records,
            )
        raise LLMInvalidOutputError(
            f"output failed validation on {1 + self._max_invalid} attempts ({request.purpose})"
        )

    async def _attempt_with_transport_retries[T: BaseModel](
        self,
        preq: ProviderRequest,
        request: LLMRequest[T],
        ctx: CallContext,
        sha: str,
        state: _CallState,
    ) -> RawCompletion:
        timeout = request.timeout_s or self._timeout_s
        last: Outcome = "transport_error"
        for retry in range(1 + self._max_transport):
            if retry:
                delay = min(self._backoff_max, self._backoff_base * 2 ** (retry - 1))
                await self._sleep(delay * self._rand())  # full jitter
            if ctx.pre_call is not None:
                await ctx.pre_call()  # may raise LLMBudgetExceededError: nothing is sent
            state.started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    return await self.provider.complete(preq)
            except TimeoutError:
                last = "timeout"
                await self._record(request, ctx, sha, state, "timeout", "TimeoutError", None)
            except ProviderError as exc:
                if not exc.retryable:
                    await self._record(
                        request, ctx, sha, state, "fatal_error", exc.error_class, None
                    )
                    raise LLMFatalError(exc.error_class) from None
                last = "transport_error"
                await self._record(
                    request, ctx, sha, state, "transport_error", exc.error_class, None
                )
        attempts = 1 + self._max_transport
        if last == "timeout":
            raise LLMTimeoutError(f"timed out after {attempts} attempts ({request.purpose})")
        raise LLMTransportError(f"transport failed after {attempts} attempts ({request.purpose})")

    async def _record[T: BaseModel](
        self,
        request: LLMRequest[T],
        ctx: CallContext,
        sha: str,
        state: _CallState,
        outcome: Outcome,
        error_class: str | None,
        raw: RawCompletion | None,
    ) -> None:
        latency_ms = int((time.monotonic() - state.started) * 1000)
        usage = (
            Usage(
                raw.input_tokens, raw.output_tokens, raw.cache_read_tokens, raw.cache_write_tokens
            )
            if raw is not None
            else Usage()
        )
        cost = cost_usd(self._price, usage) if self._price is not None else None
        state.attempts += 1
        state.usage = state.usage + usage
        state.latency_ms += latency_ms
        if cost is not None:
            state.cost = (state.cost or Decimal(0)) + cost
        record = CallRecord(
            org_id=ctx.org_id,
            investigation_id=ctx.investigation_id,
            node=ctx.node,
            purpose=request.purpose,
            provider=self.provider.name,
            model=raw.model if raw is not None else self.provider.model,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            prompt_sha256=sha,
            attempt=state.attempts,
            outcome=outcome,
            error_class=error_class,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=cost,
            provider_request_id=raw.request_id if raw is not None else None,
        )
        state.records.append(record)
        LLM_CALLS.labels(request.purpose, outcome).inc()
        LLM_LATENCY.labels(request.purpose).observe(latency_ms / 1000)
        for kind, n in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_tokens),
            ("cache_write", usage.cache_write_tokens),
        ):
            if n:
                LLM_TOKENS.labels(self.provider.name, kind).inc(n)
        await self._sink.record(record)


class _CallState:
    def __init__(self) -> None:
        self.attempts = 0
        self.usage = Usage()
        self.cost: Decimal | None = None
        self.latency_ms = 0
        self.started = time.monotonic()
        self.records: list[CallRecord] = []
