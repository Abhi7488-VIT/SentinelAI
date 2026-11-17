"""`.env.example` documents every variable the backend and docker-compose.yml read."""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]


def _example_keys() -> set[str]:
    keys: set[str] = set()
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_every_setting_is_in_env_example() -> None:
    expected = {name.upper() for name in Settings.model_fields}
    missing = expected - _example_keys()
    assert not missing, f".env.example is missing settings: {sorted(missing)}"


def test_every_compose_variable_is_in_env_example() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # ${VAR}, ${VAR:-default}, ${VAR:?message}; $$VAR is an escaped container-side variable.
    used = set(re.findall(r"(?<!\$)\$\{([A-Z_][A-Z0-9_]*)", compose))
    missing = used - _example_keys()
    assert not missing, f".env.example is missing compose variables: {sorted(missing)}"
    assert len(used) >= 10  # sanity: the regex really found the compose variables


def test_test_urls_documented() -> None:
    assert {"TEST_DATABASE_URL", "TEST_REDIS_URL"} <= _example_keys()


def test_no_real_secrets_in_example() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET"):
        value = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
        assert value is not None
        assert value.group(1).startswith("change-me"), f"{key} must be a placeholder"
