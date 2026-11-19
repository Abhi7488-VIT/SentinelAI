"""One run at a time: an exclusive lock file under runs/, with stale-lock recovery."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

LOCK_NAME = ".controller.lock"
STOP_NAME = ".stop-request"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    def __init__(self, runs_dir: Path) -> None:
        self.path = runs_dir / LOCK_NAME
        self.stop_path = runs_dir / STOP_NAME

    def current(self) -> dict[str, Any] | None:
        try:
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if data.get("mode") != "manual" and not _alive(int(data.get("pid", 0))):
            return None  # stale: the process that held it is gone
        return data

    def acquire(self, info: dict[str, Any]) -> dict[str, Any] | None:
        """Take the lock. Returns None on success, or the holder's info if it is held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                holder = self.current()
                if holder is not None:
                    return holder
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()  # stale lock
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({**info, "pid": info.get("pid", os.getpid())}, fh)
            with contextlib.suppress(FileNotFoundError):
                self.stop_path.unlink()
            return None
        holder = self.current()
        return holder or {"error": "could not take the lock"}

    def release(self, run_id: str) -> None:
        holder = self.current()
        if holder is None or holder.get("run_id") == run_id:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self.stop_path.unlink()

    def request_stop(self, run_id: str) -> None:
        self.stop_path.write_text(run_id, encoding="utf-8")

    def stop_requested(self, run_id: str) -> bool:
        try:
            return self.stop_path.read_text(encoding="utf-8").strip() == run_id
        except FileNotFoundError:
            return False
