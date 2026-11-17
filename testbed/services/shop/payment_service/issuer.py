"""The card issuer the payment service talks to, simulated in-process.

Decisions and latency are a pure function of (seed, order_id): the same order always gets the
same answer, so traffic runs are reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

DECLINE_CODES = ("insufficient_funds", "do_not_honor", "expired_card")


@dataclass(frozen=True)
class IssuerDecision:
    approved: bool
    decline_code: str | None
    latency_ms: int


def _digest(seed: int, order_id: int) -> int:
    raw = hashlib.sha256(f"{seed}:{order_id}".encode()).digest()
    return int.from_bytes(raw[:8], "big")


def decide(seed: int, order_id: int, *, decline_permille: int = 30) -> IssuerDecision:
    value = _digest(seed, order_id)
    declined = value % 1000 < decline_permille
    latency_ms = 20 + (value >> 16) % 41  # 20..60 ms
    code = DECLINE_CODES[(value >> 32) % len(DECLINE_CODES)] if declined else None
    return IssuerDecision(approved=not declined, decline_code=code, latency_ms=latency_ms)


async def authorize(seed: int, order_id: int) -> IssuerDecision:
    decision = decide(seed, order_id)
    await asyncio.sleep(decision.latency_ms / 1000)  # the network round trip to the issuer
    return decision
