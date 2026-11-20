"""Ingest API keys: generation, parsing and verification.

Format: ``sai_<prefix>_<secret>``
- ``prefix``: 12 lowercase hex chars, stored in clear and unique; used to find the key row.
- ``secret``: 32 random bytes, base64url (43 chars; may contain ``_`` and ``-``).

Only SHA-256 of the whole token is stored. The secret has 256 bits of entropy, so a fast hash is
sufficient (no offline guessing is feasible) and keeps per-request verification cheap — every
OTLP request is authenticated. Plain text is returned exactly once, at creation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

KEY_SCHEME = "sai"
_PREFIX_HEX_BYTES = 6
_SECRET_BYTES = 32
# 43 = len(base64url(32 bytes)) without padding.
_TOKEN_RE = re.compile(r"^sai_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    token: str  # shown once, never stored
    prefix: str
    secret_hash: bytes


@dataclass(frozen=True, slots=True)
class ParsedKey:
    prefix: str
    token: str


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def generate_key() -> GeneratedKey:
    prefix = secrets.token_hex(_PREFIX_HEX_BYTES)
    token = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(_SECRET_BYTES)}"
    return GeneratedKey(token=token, prefix=prefix, secret_hash=hash_token(token))


def parse_key(raw: str) -> ParsedKey | None:
    """Return the parsed key, or None if `raw` is not syntactically a SentinelAI key."""
    match = _TOKEN_RE.fullmatch(raw)
    if match is None:
        return None
    return ParsedKey(prefix=match.group(1), token=raw)


def verify_key(parsed: ParsedKey, stored_hash: bytes) -> bool:
    """Constant-time comparison of the presented token against the stored hash."""
    return hmac.compare_digest(hash_token(parsed.token), stored_hash)


def display_prefix(prefix: str) -> str:
    """What the UI shows to identify a key (never the secret)."""
    return f"{KEY_SCHEME}_{prefix}_…"
