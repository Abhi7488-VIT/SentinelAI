"""AnthropicProvider against a mocked HTTP transport: no network, no real key (plan §3, T1).

The official SDK is driven end to end through `httpx2.MockTransport` (the SDK's HTTP library),
so these tests check the request we actually put on the wire and how real SDK exceptions are
mapped.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2 as httpx  # the SDK's HTTP library (anthropic 2.x)
import pytest
from pydantic import BaseModel

from app.llm.client import LLMClient
from app.llm.errors import LLMFatalError, LLMRefusalError, LLMTransportError, ProviderError
from app.llm.pricing import DEFAULT_PRICES
from app.llm.providers.anthropic import FALLBACK_BETA, AnthropicProvider
from app.llm.types import CallContext, CallRecord, LLMRequest, ProviderRequest

FAKE_KEY = "sk-ant-test-" + "q" * 40
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"claim": {"type": "string"}},
    "required": ["claim"],
    "additionalProperties": False,
}
PREQ = ProviderRequest(
    model="claude-sonnet-5-5",
    system="You are a test.",
    user_blocks=("block one", "block two"),
    json_schema=SCHEMA,
    max_output_tokens=512,
)

Handler = Callable[[httpx.Request], httpx.Response]


class Captured:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def body(self, i: int = -1) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[i].content)
        return result


def message(
    text: str = '{"claim": "pool saturated"}',
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 0,
        },
    }


def ok(body: dict[str, Any]) -> Handler:
    return lambda _req: httpx.Response(200, json=body, headers={"request-id": "req_abc123"})


def status(code: int) -> Handler:
    # Error bodies may echo request content; the provider must never surface it.
    body = {
        "type": "error",
        "error": {"type": "api_error", "message": f"echo {FAKE_KEY} block one"},
    }
    return lambda _req: httpx.Response(code, json=body)


def provider(
    handler: Handler, model: str = "claude-sonnet-5-5"
) -> tuple[AnthropicProvider, Captured]:
    captured = Captured()

    def record(req: httpx.Request) -> httpx.Response:
        captured.requests.append(req)
        return handler(req)

    sdk = anthropic.AsyncAnthropic(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    return AnthropicProvider(
        api_key=FAKE_KEY, model=model, effort="medium", timeout_s=5.0, client=sdk
    ), captured


async def test_request_on_the_wire_uses_structured_output_and_server_side_fallback() -> None:
    p, cap = provider(ok(message()))
    await p.complete(PREQ)

    req = cap.requests[0]
    assert req.url.path == "/v1/messages"
    assert FALLBACK_BETA in req.headers["anthropic-beta"]
    assert req.headers["x-stainless-retry-count"] == "0"
    body = cap.body()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 512
    assert body["system"] == "You are a test."
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "block one"},
                {"type": "text", "text": "block two"},
            ],
        }
    ]


async def test_model_without_fallback_support_uses_the_plain_endpoint() -> None:
    p, cap = provider(ok(message(model="claude-haiku-5-5")), model="claude-haiku-5-5")
    await p.complete(PREQ)
    assert "anthropic-beta" not in cap.requests[0].headers
    assert "fallbacks" not in cap.body()


async def test_response_is_mapped_to_raw_completion() -> None:
    p, _ = provider(ok(message()))
    raw = await p.complete(PREQ)
    assert raw.text == '{"claim": "pool saturated"}'
    assert raw.stop_reason == "end_turn"
    assert raw.model == "claude-sonnet-5-5"
    assert (raw.input_tokens, raw.output_tokens) == (120, 30)
    assert (raw.cache_read_tokens, raw.cache_write_tokens) == (7, 0)
    assert raw.request_id == "req_abc123"


@pytest.mark.parametrize(
    ("stop", "expected"),
    [("refusal", "refusal"), ("max_tokens", "max_tokens"), ("pause_turn", "other")],
)
async def test_stop_reasons(stop: str, expected: str) -> None:
    p, _ = provider(ok(message("", stop_reason=stop)))
    raw = await p.complete(PREQ)
    assert raw.stop_reason == expected


@pytest.mark.parametrize(
    ("code", "error_class", "retryable"),
    [
        (429, "RateLimitError", True),
        (500, "InternalServerError", True),
        (529, "InternalServerError", True),
        (409, "ConflictError", True),
        (400, "BadRequestError", False),
        (401, "AuthenticationError", False),
        (403, "PermissionDeniedError", False),
        (404, "NotFoundError", False),
    ],
)
async def test_http_errors_map_to_provider_errors(
    code: int, error_class: str, retryable: bool
) -> None:
    p, cap = provider(status(code))
    with pytest.raises(ProviderError) as info:
        await p.complete(PREQ)
    assert info.value.error_class == error_class
    assert info.value.retryable is retryable
    assert len(cap.requests) == 1  # SDK retries are off; LLMClient owns the cap
    # Only the class name travels on: no response body, no key, no prompt text.
    assert str(info.value) == error_class
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


async def test_connection_error_is_retryable() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed", request=req)

    p, _ = provider(boom)
    with pytest.raises(ProviderError) as info:
        await p.complete(PREQ)
    assert (info.value.error_class, info.value.retryable) == ("APIConnectionError", True)


async def test_read_timeout_is_retryable() -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    p, _ = provider(slow)
    with pytest.raises(ProviderError) as info:
        await p.complete(PREQ)
    assert (info.value.error_class, info.value.retryable) == ("APITimeoutError", True)


# --- through LLMClient: retry policy and secrets with the real SDK in the loop ---------------


class Claim(BaseModel):
    claim: str


class Sink:
    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    async def record(self, record: CallRecord) -> None:
        self.records.append(record)


async def _no_sleep(_delay: float) -> None:
    return None


def client_for(p: AnthropicProvider) -> tuple[LLMClient, Sink]:
    sink = Sink()
    return LLMClient(
        p,
        sink,
        dict(DEFAULT_PRICES),
        timeout_s=5.0,
        max_invalid_retries=2,
        max_transport_retries=3,
        max_input_tokens=30_000,
        sleep=_no_sleep,
        rand=lambda: 0.5,
    ), sink


LREQ: LLMRequest[Claim] = LLMRequest(
    purpose="analyst",
    prompt_id="test",
    prompt_version=1,
    system="You are a test.",
    user_blocks=("block one",),
    output_model=Claim,
)
CTX = CallContext(org_id=uuid.uuid4(), node="analyst")


async def test_client_parses_a_real_sdk_response_and_prices_it() -> None:
    p, _ = provider(ok(message()))
    client, sink = client_for(p)
    result = await client.generate(LREQ, CTX)
    assert result.parsed == Claim(claim="pool saturated")
    assert result.provider == "anthropic"
    assert result.provider_request_id == "req_abc123"
    assert result.cost_usd is not None
    assert result.cost_usd > 0
    assert [r.outcome for r in sink.records] == ["ok"]


async def test_client_retries_429_up_to_the_cap() -> None:
    p, cap = provider(status(429))
    client, sink = client_for(p)
    with pytest.raises(LLMTransportError):
        await client.generate(LREQ, CTX)
    assert len(cap.requests) == 4  # 1 + LLM_MAX_TRANSPORT_RETRIES
    assert {r.error_class for r in sink.records} == {"RateLimitError"}


async def test_client_does_not_retry_400() -> None:
    p, cap = provider(status(400))
    client, sink = client_for(p)
    with pytest.raises(LLMFatalError):
        await client.generate(LREQ, CTX)
    assert len(cap.requests) == 1
    assert [r.outcome for r in sink.records] == ["fatal_error"]


async def test_client_records_a_final_refusal_without_inventing_output() -> None:
    p, cap = provider(ok(message("", stop_reason="refusal")))
    client, sink = client_for(p)
    with pytest.raises(LLMRefusalError):
        await client.generate(LREQ, CTX)
    assert len(cap.requests) == 1
    assert [r.outcome for r in sink.records] == ["refusal"]


async def test_api_key_and_prompt_never_reach_errors_records_or_logs(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # Root at DEBUG, as with LOG_LEVEL=DEBUG; the SDK would otherwise log request bodies.
    caplog.set_level(logging.DEBUG)
    for handler in (status(400), status(401), status(429), status(500), ok(message())):
        p, cap = provider(handler)
        client, sink = client_for(p)
        errors: list[str] = []
        try:
            await client.generate(LREQ, CTX)
        except (LLMFatalError, LLMTransportError) as exc:
            errors += [str(exc), repr(exc)]
        for text in errors:
            assert FAKE_KEY not in text
            assert "block one" not in text
        # The key is only ever in the auth header of the outgoing request.
        assert cap.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
        assert all(FAKE_KEY not in repr(r) for r in sink.records)
        assert all("block one" not in repr(r) for r in sink.records)
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert FAKE_KEY not in text
        assert "block one" not in text
        assert "You are a test." not in text
