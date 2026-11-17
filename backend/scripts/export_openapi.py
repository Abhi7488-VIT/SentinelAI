"""Write the API's OpenAPI document to a file (input for frontend type generation).

Usage: uv run python -m scripts.export_openapi ../frontend/openapi.json
Needs no database or env: it only builds the app and serializes its schema.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


def main(out_path: str) -> None:
    settings = Settings(
        _env_file=None,
        env="dev",
        database_url=SecretStr("postgresql+asyncpg://unused:unused@localhost/unused"),
        redis_url=SecretStr("redis://localhost/0"),
        celery_broker_url=SecretStr("redis://localhost/1"),
        celery_result_backend=SecretStr("redis://localhost/2"),
        jwt_secret=SecretStr("openapi-export-only-" + "x" * 32),
    )
    schema = create_app(settings).openapi()
    Path(out_path).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
