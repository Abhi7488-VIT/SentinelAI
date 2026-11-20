"""Cost per call, computed from list prices (USD per million tokens).

Defaults are Anthropic's published list prices as recorded on 2026-11-03 (input / output / cache
read); cache writes are priced at 1.25 x input, the documented 5-minute cache-write multiplier.
These are list prices, not an invoice: reports label costs "computed from list price".
`LLM_PRICES_JSON` overrides the table. A model without a price cannot be used with the real
provider (cost budgets could not be enforced), see `LLMNotConfiguredError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from app.llm.types import Usage

PRICES_AS_OF = "2026-11-03"
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


def _price(inp: str, out: str, cache_read: str) -> ModelPrice:
    i = Decimal(inp)
    return ModelPrice(i, Decimal(out), Decimal(cache_read), i * Decimal("1.25"))


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": _price("4.00", "20.00", "0.20"),
    "claude-sonnet-5-5": _price("2.00", "10.00", "0.10"),
    # Haiku 5.5: no cache-read list price was recorded; 0.1 x input (the documented cache-read
    # multiplier) is used. Prices above 100k-token prompts ($0.50 / $2.50) are not modelled:
    # the client caps a prompt at LLM_MAX_INPUT_TOKENS (default 30k).
    "claude-haiku-5-5": _price("0.10", "0.50", "0.01"),
}


def load_prices(override_json: str | None) -> dict[str, ModelPrice]:
    """`{"model": {"input": "2", "output": "10", "cache_read": "0.1", "cache_write": "2.5"}}`."""
    prices = dict(DEFAULT_PRICES)
    if override_json:
        raw: dict[str, dict[str, str]] = json.loads(override_json)
        for model, p in raw.items():
            prices[model] = ModelPrice(
                Decimal(str(p["input"])),
                Decimal(str(p["output"])),
                Decimal(str(p.get("cache_read", "0"))),
                Decimal(str(p.get("cache_write", p["input"]))),
            )
    return prices


def cost_usd(price: ModelPrice, usage: Usage) -> Decimal:
    total = (
        price.input * usage.input_tokens
        + price.output * usage.output_tokens
        + price.cache_read * usage.cache_read_tokens
        + price.cache_write * usage.cache_write_tokens
    ) / _MILLION
    return total.quantize(Decimal("0.000001"))
