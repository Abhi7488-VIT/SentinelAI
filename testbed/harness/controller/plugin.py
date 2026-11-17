"""Scenario plugins: a trigger and a stop composed from primitives, auto-discovered.

Adding a scenario = a definition `testbed/scenarios/<id>.json` plus a module in
`harness/controller/scenarios/` exposing `PLUGIN`; tests pick both up automatically.
"""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from typing import ClassVar

from harness.controller import scenarios as scenario_pkg
from harness.controller.primitives import Context


class Scenario(ABC):
    id: ClassVar[str]

    async def reset(self, ctx: Context) -> None:  # noqa: B027  (optional hook)
        """Bring the target to its healthy state before the baseline (idempotent, quiet)."""

    @abstractmethod
    async def trigger(self, ctx: Context) -> None:
        """Inject the fault."""

    @abstractmethod
    async def stop(self, ctx: Context) -> None:
        """Remove the fault (idempotent)."""


def discover() -> dict[str, Scenario]:
    found: dict[str, Scenario] = {}
    for info in pkgutil.iter_modules(scenario_pkg.__path__):
        module = importlib.import_module(f"{scenario_pkg.__name__}.{info.name}")
        plugin = getattr(module, "PLUGIN", None)
        if isinstance(plugin, Scenario):
            if plugin.id in found:
                raise ValueError(f"duplicate scenario id {plugin.id!r}")
            found[plugin.id] = plugin
    return dict(sorted(found.items()))
