"""Slug generation always satisfies the DB CHECK constraint."""

from __future__ import annotations

import pytest

from app.core.slugs import MAX_SLUG_LENGTH, is_valid_slug, slugify, with_random_suffix


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Corp", "acme-corp"),
        ("  --Hello__World!!  ", "hello-world"),
        ("Café Société", "cafe-societe"),
        ("Payments v2.0", "payments-v2-0"),
        ("!!!", "item"),
        ("日本語", "item"),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected
    assert is_valid_slug(slugify(raw))


def test_slugify_truncates_without_trailing_hyphen() -> None:
    slug = slugify("a" * 62 + " b")
    assert len(slug) <= MAX_SLUG_LENGTH
    assert is_valid_slug(slug)


def test_random_suffix_stays_valid_and_bounded() -> None:
    base = "x" * MAX_SLUG_LENGTH
    suffixed = with_random_suffix(base)
    assert len(suffixed) <= MAX_SLUG_LENGTH
    assert is_valid_slug(suffixed)
    assert with_random_suffix("acme") != with_random_suffix("acme")
