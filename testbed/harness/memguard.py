"""Memory watchdog: refuse to start, or stop, the testbed when the host runs low on memory.

    python -m harness.memguard check
        exit 0 if available memory >= floor, exit 3 otherwise
    python -m harness.memguard watch [--interval 2] [--on-breach "make testbed-down"] -- CMD ...
        run CMD; sample memory every interval; on breach run the --on-breach command,
        terminate CMD and exit 3. Otherwise exit with CMD's exit code.
        (--on-breach is split with POSIX shell rules, as on Linux.)

Floor: TB_MIN_AVAILABLE_MB (MiB) overrides the default for the detected host:
    Linux (Codespaces, CI, WSL):  MemAvailable from /proc/meminfo, default floor 1024 MiB
    Windows host:                 available physical memory, default floor 600 MiB

Standard library only, so it runs with any Python 3.12 before the project venv exists.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

ENV_FLOOR = "TB_MIN_AVAILABLE_MB"
EXIT_LOW_MEMORY = 3


class MemorySource(Protocol):
    kind: str  # "linux" | "windows"
    description: str
    default_floor_mb: int

    def available_mb(self) -> int: ...


class LinuxMeminfo:
    kind = "linux"
    description = "MemAvailable (/proc/meminfo)"
    default_floor_mb = 1024

    def __init__(self, path: Path = Path("/proc/meminfo")) -> None:
        self.path = path

    def available_mb(self) -> int:
        return parse_mem_available_mb(self.path.read_text(encoding="ascii"))


class WindowsMemory:
    kind = "windows"
    description = "Windows available physical memory"
    default_floor_mb = 600

    def available_mb(self) -> int:
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009  (absent off Windows)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return int(status.ullAvailPhys // (1024 * 1024))


def parse_mem_available_mb(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            value_kb = int(parts[1])
            unit = parts[2] if len(parts) > 2 else "kB"
            if unit != "kB":
                raise ValueError(f"unexpected unit in /proc/meminfo: {unit!r}")
            return value_kb // 1024
    raise ValueError("MemAvailable not found in /proc/meminfo")


def detect_source(platform: str = sys.platform) -> MemorySource:
    if platform == "win32":
        return WindowsMemory()
    return LinuxMeminfo()


def floor_mb(source: MemorySource, env: Mapping[str, str] = os.environ) -> int:
    raw = env.get(ENV_FLOOR, "").strip()
    if not raw:
        return source.default_floor_mb
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{ENV_FLOOR} must be a whole number of MiB, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{ENV_FLOOR} must not be negative, got {value}")
    return value


@dataclass
class Watchdog:
    source: MemorySource
    floor: int
    on_breach: Callable[[int], None]
    interval_s: float = 2.0
    samples: list[int] = field(default_factory=list)
    tripped: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    @property
    def min_mb(self) -> int | None:
        return min(self.samples) if self.samples else None

    def sample_once(self) -> bool:
        """Take one sample; returns True (and fires on_breach once) if below the floor."""
        mb = self.source.available_mb()
        self.samples.append(mb)
        if mb < self.floor and not self.tripped.is_set():
            self.tripped.set()
            self.on_breach(mb)
            return True
        return False

    def _run(self) -> None:
        while not self._stop.is_set() and not self.tripped.is_set():
            self.sample_once()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="memguard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 1)


def _log(message: str) -> None:
    sys.stderr.write(f"[memguard] {message}\n")
    sys.stderr.flush()


def check(source: MemorySource, floor: int) -> int:
    available = source.available_mb()
    ok = available >= floor
    _log(
        f"{source.description}: {available} MiB (floor {floor} MiB) -> {'ok' if ok else 'TOO LOW'}"
    )
    return 0 if ok else EXIT_LOW_MEMORY


def watch(
    source: MemorySource,
    floor: int,
    command: Sequence[str],
    *,
    on_breach_command: Sequence[str] | None,
    interval_s: float,
) -> int:
    if check(source, floor) != 0:
        _log("not starting: memory is already below the floor")
        return EXIT_LOW_MEMORY
    proc = subprocess.Popen(list(command))  # noqa: S603  (operator-supplied command)

    def breach(mb: int) -> None:
        _log(f"{source.description} fell to {mb} MiB (< {floor} MiB): stopping")
        try:
            if on_breach_command:
                subprocess.run(list(on_breach_command), check=False)  # noqa: S603
        except OSError as exc:
            _log(f"breach action failed: {exc}")
        finally:
            proc.terminate()  # always stop the watched command, even if the action failed

    dog = Watchdog(source, floor, breach, interval_s=interval_s)
    dog.start()
    try:
        code = proc.wait()
    finally:
        dog.stop()
    _log(f"lowest {source.description} during the run: {dog.min_mb} MiB")
    return EXIT_LOW_MEMORY if dog.tripped.is_set() else code


def main(argv: Sequence[str] | None = None, source: MemorySource | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.memguard")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="exit 0 if available memory is above the floor")
    w = sub.add_parser("watch", help="run a command under the watchdog")
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument(
        "--on-breach", default="", help='command run on breach, e.g. "make testbed-down"'
    )
    w.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    src = source or detect_source()
    try:
        floor = floor_mb(src)
    except ValueError as exc:
        _log(str(exc))
        return 2
    if args.action == "check":
        return check(src, floor)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("watch needs a command after --")
    breach_cmd = shlex.split(args.on_breach) if args.on_breach else None
    return watch(src, floor, command, on_breach_command=breach_cmd, interval_s=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
