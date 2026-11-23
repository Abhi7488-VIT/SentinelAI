"""Versioned system prompts (plan §7). Each file is `<id>.v<version>.txt`; changing a prompt means
adding a new version file, so stored `llm_calls.prompt_version` always identifies the exact text.

Released versions are pinned in `manifest.json` (SHA-256 of the text): `load_prompt` refuses a
file whose text no longer matches, so editing a released prompt in place fails at runtime and in
CI; a change must be a new version file plus a manifest entry.

Every prompt starts with a `SentinelAI prompt: <id> v<version>` line (also lets the fake provider
route by prompt in tests) and states that `untrusted_telemetry` blocks are data, never
instructions (invariant 3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    version: int
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


class PromptIntegrityError(RuntimeError):
    pass


@cache
def manifest() -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = json.loads(
        (PROMPTS_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    return data


@cache
def load_prompt(prompt_id: str, version: int) -> Prompt:
    path = PROMPTS_DIR / f"{prompt_id}.v{version}.txt"
    text = path.read_text(encoding="utf-8")
    header = f"SentinelAI prompt: {prompt_id} v{version}"
    if not text.startswith(header):
        raise PromptIntegrityError(f"{path.name} must start with {header!r}")
    prompt = Prompt(prompt_id, version, text)
    pinned = manifest().get(prompt_id, {}).get(str(version))
    if pinned != prompt.sha256:
        raise PromptIntegrityError(f"{path.name} does not match its pinned hash in manifest.json")
    return prompt
