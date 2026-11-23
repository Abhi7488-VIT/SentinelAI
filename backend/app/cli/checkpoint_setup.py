"""Create the LangGraph checkpoint schema and tables (idempotent). Run after `alembic upgrade`."""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.investigation.checkpoint import setup_checkpointer


def main() -> None:
    asyncio.run(setup_checkpointer(get_settings().database_url.get_secret_value()))
    print("checkpoint schema ready")  # noqa: T201


if __name__ == "__main__":
    main()
