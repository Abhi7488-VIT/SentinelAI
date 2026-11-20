"""Build the configured `LLMClient` from settings."""

from __future__ import annotations

from app.core.config import Settings
from app.llm.client import LLMClient
from app.llm.errors import LLMNotConfiguredError
from app.llm.pricing import load_prices
from app.llm.providers.fake import FakeProvider
from app.llm.types import Provider, UsageSink


def build_provider(settings: Settings, fake: FakeProvider | None = None) -> Provider:
    if settings.llm_provider == "fake":
        return fake or FakeProvider()
    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value():
        raise LLMNotConfiguredError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
    from app.llm.providers.anthropic import AnthropicProvider  # SDK imported only when used

    return AnthropicProvider(
        api_key=key.get_secret_value(),
        model=settings.llm_model,
        effort=settings.llm_effort,
        timeout_s=settings.llm_timeout_seconds,
    )


def build_client(
    settings: Settings, sink: UsageSink, fake: FakeProvider | None = None
) -> LLMClient:
    return LLMClient(
        build_provider(settings, fake),
        sink,
        load_prices(settings.llm_prices_json),
        timeout_s=settings.llm_timeout_seconds,
        max_invalid_retries=settings.llm_max_invalid_retries,
        max_transport_retries=settings.llm_max_transport_retries,
        max_input_tokens=settings.llm_max_input_tokens,
    )
