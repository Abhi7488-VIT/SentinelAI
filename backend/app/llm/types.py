"""Request/response types for the provider interface (Phase 4 plan §3)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel

Purpose = Literal["planner", "telemetry", "dependency", "analyst", "report"]
StopReason = Literal["end_turn", "max_tokens", "refusal", "other"]
Outcome = Literal[
    "ok",
    "invalid_output",
    "timeout",
    "transport_error",
    "fatal_error",
    "refusal",
]


@dataclass(frozen=True)
class LLMRequest[T: BaseModel]:
    """One structured call. `user_blocks` are already-built text blocks; untrusted content in
    them must already be wrapped by the prompt builder (invariant 3)."""

    purpose: Purpose
    prompt_id: str
    prompt_version: int
    system: str
    user_blocks: tuple[str, ...]
    output_model: type[T]
    max_output_tokens: int = 8000
    timeout_s: float | None = None  # None → settings default


@dataclass(frozen=True)
class CallContext:
    """Who the call is for. `pre_call` lets the caller enforce budgets before each attempt;
    it raises `LLMBudgetExceededError` to stop the call without sending anything."""

    org_id: uuid.UUID
    investigation_id: uuid.UUID | None = None
    node: str = ""
    pre_call: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str
    user_blocks: tuple[str, ...]
    json_schema: dict[str, object]
    max_output_tokens: int


@dataclass(frozen=True)
class RawCompletion:
    text: str
    stop_reason: StopReason
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_id: str | None = None


class Provider(Protocol):
    name: str  # "anthropic" | "fake"
    model: str

    async def complete(self, request: ProviderRequest) -> RawCompletion: ...


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class CallRecord:
    """One attempt, successful or not. Stored in `llm_calls`; contains no prompt or output text."""

    org_id: uuid.UUID
    investigation_id: uuid.UUID | None
    node: str
    purpose: str
    provider: str
    model: str
    prompt_id: str
    prompt_version: int
    prompt_sha256: str
    attempt: int
    outcome: Outcome
    error_class: str | None
    usage: Usage
    latency_ms: int
    cost_usd: Decimal | None
    provider_request_id: str | None


class UsageSink(Protocol):
    async def record(self, record: CallRecord) -> None: ...


@dataclass
class LLMResult[T: BaseModel]:
    parsed: T
    usage: Usage  # summed over every attempt of this call, failed ones included
    cost_usd: Decimal | None
    latency_ms: int
    model: str
    provider: str
    attempts: int
    provider_request_id: str | None
    records: list[CallRecord] = field(default_factory=list)
