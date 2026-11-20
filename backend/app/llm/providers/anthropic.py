"""Real provider: Anthropic Messages API through the official async SDK.

- The API key is read from settings (`ANTHROPIC_API_KEY`, env only) and passed straight to the
  SDK; it is never logged, stored or put into an error message.
- SDK retries are disabled (`max_retries=0`): `LLMClient` owns the retry cap and its tests.
- Structured output via `output_config.format` (JSON schema from `app.llm.schema`); the client
  re-validates with Pydantic regardless.
- Server-side refusal fallback (`fallbacks: "default"`) on the models that support it.
- Errors are mapped by typed SDK exception class, most specific first; only the class name
  travels on (`ProviderError.error_class`), never the response body.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from app.llm.errors import ProviderError
from app.llm.types import ProviderRequest, RawCompletion, StopReason

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept server-side refusal fallback (Haiku 5.5 has none).
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
_RETRYABLE_STATUS = {408, 409, 429}
_SDK_LOGGERS = ("anthropic", "httpx2")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        effort: str,
        timeout_s: float,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # The SDK logs full request bodies (prompt text = telemetry excerpts) at DEBUG, which
        # `LOG_LEVEL=DEBUG` or `ANTHROPIC_LOG=debug` would turn on. Prompts are never logged
        # (D7), so its loggers are pinned at WARNING whatever the app or env asks for.
        for name in _SDK_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        self.model = model
        self._effort = effort
        self._fallback = model in FALLBACK_MODELS
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=0, timeout=timeout_s
        )

    async def complete(self, request: ProviderRequest) -> RawCompletion:
        messages: list[Any] = [
            {
                "role": "user",
                "content": [{"type": "text", "text": block} for block in request.user_blocks],
            }
        ]
        output_config: Any = {
            "effort": self._effort,
            "format": {"type": "json_schema", "schema": request.json_schema},
        }
        try:
            if self._fallback:
                response: Any = await self._client.beta.messages.create(
                    model=self.model,
                    max_tokens=request.max_output_tokens,
                    system=request.system,
                    messages=messages,
                    output_config=output_config,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            else:
                response = await self._client.messages.create(
                    model=self.model,
                    max_tokens=request.max_output_tokens,
                    system=request.system,
                    messages=messages,
                    output_config=output_config,
                )
        except anthropic.RateLimitError:
            raise ProviderError("RateLimitError", retryable=True) from None
        except anthropic.InternalServerError:
            raise ProviderError("InternalServerError", retryable=True) from None
        except anthropic.APITimeoutError:
            raise ProviderError("APITimeoutError", retryable=True) from None
        except anthropic.APIConnectionError:
            raise ProviderError("APIConnectionError", retryable=True) from None
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code in _RETRYABLE_STATUS or exc.status_code >= 500
            raise ProviderError(type(exc).__name__, retryable=retryable) from None

        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        usage = response.usage
        return RawCompletion(
            text=text,
            stop_reason=_stop_reason(response.stop_reason),
            model=str(response.model),
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            request_id=getattr(response, "_request_id", None),
        )


def _stop_reason(value: str | None) -> StopReason:
    if value in ("end_turn", "max_tokens", "refusal"):
        return value  # type: ignore[return-value]
    return "other"
