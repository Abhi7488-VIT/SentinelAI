"""The releases manifest: which version (and config) each service runs.

Written by the deploy pipeline; read once at process start. A missing file or entry means the
service runs the version given by its SERVICE_VERSION environment variable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Release:
    version: str
    config: dict[str, str] = field(default_factory=dict)


def active_release(path: Path, service: str) -> Release | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    entry = manifest.get("services", {}).get(service)
    if not entry:
        return None
    return Release(
        version=str(entry["version"]),
        config={str(k): str(v) for k, v in entry.get("config", {}).items()},
    )
