"""Memory watchdog: floor configuration, /proc/meminfo parsing, breach handling."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.memguard import (
    ENV_FLOOR,
    EXIT_LOW_MEMORY,
    LinuxMeminfo,
    Watchdog,
    WindowsMemory,
    detect_source,
    floor_mb,
    main,
    parse_mem_available_mb,
)

MEMINFO = """MemTotal:       16384000 kB
MemFree:         1024000 kB
MemAvailable:    3145728 kB
Buffers:          102400 kB
"""


class FakeSource:
    kind = "linux"
    description = "MemAvailable (/proc/meminfo)"
    default_floor_mb = 1024

    def __init__(self, values: list[int]) -> None:
        self._values: Iterator[int] = iter(values)
        self._last = values[-1]

    def available_mb(self) -> int:
        self._last = next(self._values, self._last)
        return self._last


def test_parse_mem_available() -> None:
    assert parse_mem_available_mb(MEMINFO) == 3072  # 3145728 kB


def test_parse_rejects_missing_field() -> None:
    with pytest.raises(ValueError, match="MemAvailable not found"):
        parse_mem_available_mb("MemTotal: 1 kB\n")


def test_linux_source_reads_proc_meminfo_file(tmp_path: Path) -> None:
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO, encoding="ascii")
    source = LinuxMeminfo(path)
    assert source.available_mb() == 3072
    assert "Windows" not in source.description


def test_detect_source_by_platform() -> None:
    assert isinstance(detect_source("linux"), LinuxMeminfo)
    assert isinstance(detect_source("win32"), WindowsMemory)


@pytest.mark.parametrize(
    ("source", "env", "expected"),
    [
        (LinuxMeminfo(), {}, 1024),
        (WindowsMemory(), {}, 600),
        (LinuxMeminfo(), {ENV_FLOOR: "2048"}, 2048),
        (WindowsMemory(), {ENV_FLOOR: "800"}, 800),
        (LinuxMeminfo(), {ENV_FLOOR: "  "}, 1024),
    ],
)
def test_floor_defaults_and_override(
    source: LinuxMeminfo | WindowsMemory, env: dict[str, str], expected: int
) -> None:
    assert floor_mb(source, env) == expected


@pytest.mark.parametrize("bad", ["lots", "1.5", "-1"])
def test_invalid_floor_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match=ENV_FLOOR):
        floor_mb(LinuxMeminfo(), {ENV_FLOOR: bad})


def test_watchdog_fires_once_below_floor() -> None:
    breaches: list[int] = []
    dog = Watchdog(FakeSource([2000, 1500, 900, 800]), floor=1024, on_breach=breaches.append)
    assert dog.sample_once() is False
    assert dog.sample_once() is False
    assert dog.sample_once() is True
    assert dog.sample_once() is False  # already tripped: no second breach action
    assert breaches == [900]
    assert dog.min_mb == 800


def test_check_command_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLOOR, raising=False)
    assert main(["check"], source=FakeSource([4096])) == 0
    assert main(["check"], source=FakeSource([512])) == EXIT_LOW_MEMORY
    monkeypatch.setenv(ENV_FLOOR, "256")
    assert main(["check"], source=FakeSource([512])) == 0
    monkeypatch.setenv(ENV_FLOOR, "nope")
    assert main(["check"], source=FakeSource([512])) == 2


def test_watch_runs_command_when_memory_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLOOR, raising=False)
    code = main(
        ["watch", "--interval", "0.05", "--", sys.executable, "-c", "raise SystemExit(7)"],
        source=FakeSource([4096]),
    )
    assert code == 7  # the command's own exit code is passed through


def test_watch_stops_command_and_runs_breach_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_FLOOR, raising=False)
    marker = tmp_path / "stopped"
    # --on-breach is split with POSIX rules (as on Linux), so use forward-slash paths.
    python = Path(sys.executable).as_posix()
    breach = f"{python} -c \"open('{marker.as_posix()}', 'w').close()\""
    code = main(
        [
            "watch",
            "--interval",
            "0.05",
            "--on-breach",
            breach,
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        source=FakeSource([4096, 4096, 300]),
    )
    assert code == EXIT_LOW_MEMORY
    assert marker.exists()


def test_watch_refuses_to_start_when_already_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLOOR, raising=False)
    code = main(
        ["watch", "--", sys.executable, "-c", "raise SystemExit(0)"], source=FakeSource([100])
    )
    assert code == EXIT_LOW_MEMORY
