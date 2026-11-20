"""Deterministic scripted provider for tests, CI and development without a key.

Token counts are NOT real: input = characters / 4 of the request, output = characters / 4 of the
response. Everything downstream labels them with `provider = "fake"`; reports must say
"fake provider counts", never present them as real usage.

A script is a sequence of steps consumed one per attempt, or a `responder` that picks a step from
the request (e.g. by the purpose named in the system prompt). Every received request is kept in
`requests` so tests can inspect exactly what would have been sent.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.llm.errors import ProviderError
from app.llm.types import ProviderRequest, RawCompletion, StopReason

FAKE_MODEL = "fake-model"


@dataclass(frozen=True)
class Respond:
    """Return this payload as the model's text (a model instance, dict, or a raw string)."""

    payload: BaseModel | Mapping[str, object] | str
    stop_reason: StopReason = "end_turn"

    def text(self) -> str:
        if isinstance(self.payload, BaseModel):
            return self.payload.model_dump_json()
        if isinstance(self.payload, Mapping):
            return json.dumps(dict(self.payload))
        return self.payload


@dataclass(frozen=True)
class Hang:
    """Never answer (exercises the client's timeout)."""


@dataclass(frozen=True)
class Fail:
    error_class: str = "RateLimitError"
    retryable: bool = True


FakeStep = Respond | Hang | Fail


@dataclass
class FakeProvider:
    script: Iterable[FakeStep] = ()
    responder: Callable[[ProviderRequest], FakeStep] | None = None
    model: str = FAKE_MODEL
    name: str = "fake"
    requests: list[ProviderRequest] = field(default_factory=list)
    _counter: itertools.count[int] = field(default_factory=itertools.count)

    def __post_init__(self) -> None:
        self._steps = iter(self.script)

    async def complete(self, request: ProviderRequest) -> RawCompletion:
        self.requests.append(request)
        n = next(self._counter)
        step = self.responder(request) if self.responder is not None else next(self._steps, None)
        if step is None:
            raise AssertionError("FakeProvider script exhausted")
        if isinstance(step, Hang):
            await asyncio.Event().wait()
        if isinstance(step, Fail):
            raise ProviderError(step.error_class, retryable=step.retryable)
        if not isinstance(step, Respond):
            raise TypeError(f"unknown fake step {step!r}")
        text = step.text()
        sent = len(request.system) + sum(len(b) for b in request.user_blocks)
        return RawCompletion(
            text=text,
            stop_reason=step.stop_reason,
            model=self.model,
            input_tokens=sent // 4 + 1,
            output_tokens=len(text) // 4 + 1,
            request_id=f"fake-{n}",
        )
