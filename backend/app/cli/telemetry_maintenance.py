"""Create/drop telemetry partitions once (run by the `migrate` container after migrations).

Usage: python -m app.cli.telemetry_maintenance
"""

from __future__ import annotations

import time
from dataclasses import asdict

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.telemetry_maintenance import run_maintenance
from app.worker.db import run_with_session


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    for _ in range(30):  # another run (beat) may hold the lock briefly
        result = run_with_session(
            settings,
            lambda session: run_maintenance(session, settings),
            application_name="sentinelai-maintenance",
        )
        if result.ran:
            break
        time.sleep(1)
    else:
        raise SystemExit("telemetry maintenance skipped: another run held the lock for 30 s")
    print(asdict(result))  # noqa: T201  (CLI output)


if __name__ == "__main__":
    main()
