"""Slug helpers matching the DB CHECK `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`."""

from __future__ import annotations

import re
import secrets
import unicodedata

MAX_SLUG_LENGTH = 63
SLUG_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def slugify(value: str, *, fallback: str = "item") -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    slug = slug[:MAX_SLUG_LENGTH].strip("-")
    return slug or fallback


def with_random_suffix(slug: str) -> str:
    suffix = secrets.token_hex(3)
    base = slug[: MAX_SLUG_LENGTH - len(suffix) - 1].strip("-")
    return f"{base}-{suffix}" if base else suffix


def is_valid_slug(value: str) -> bool:
    return bool(SLUG_REGEX.match(value))
