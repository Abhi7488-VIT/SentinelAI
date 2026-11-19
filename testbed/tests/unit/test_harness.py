"""Load-generator determinism and the deploy pipeline (no running services needed)."""

from __future__ import annotations

import itertools
import json
import statistics
from collections import Counter
from pathlib import Path

import httpx
import pytest

from harness.deploy import EventSink, deploy, read_manifest
from harness.loadgen.mixes import BASELINE_WEIGHTS, Mix, Stream, get_mix
from harness.loadgen.schedule import (
    SKUS,
    VIRTUAL_USERS,
    stream_events,
    user_events,
    virtual_users,
)
from harness.loadgen.stats import Stats, percentile

# --- schedules ----------------------------------------------------------------------------


def _take(gen: object, n: int) -> list[object]:
    return list(itertools.islice(gen, n))  # type: ignore[call-overload]


def test_same_seed_same_schedule() -> None:
    mix = get_mix("baseline")
    assert _take(user_events(42, mix), 500) == _take(user_events(42, mix), 500)
    assert _take(user_events(42, mix), 500) != _take(user_events(43, mix), 500)


def test_arrival_rate_and_action_weights() -> None:
    mix = get_mix("baseline")
    events = _take(user_events(42, mix), 20_000)
    gaps = [b.at - a.at for a, b in itertools.pairwise(events)]  # type: ignore[attr-defined]
    assert statistics.fmean(gaps) == pytest.approx(1 / mix.rps, rel=0.03)
    counts = Counter(e.action for e in events)  # type: ignore[attr-defined]
    total_weight = sum(BASELINE_WEIGHTS.values())
    for action, weight in BASELINE_WEIGHTS.items():
        assert counts[action] / len(events) == pytest.approx(weight / total_weight, abs=0.01)


def test_events_reference_real_users_and_products() -> None:
    users = set(virtual_users(42))
    assert len(users) == VIRTUAL_USERS
    assert users == set(virtual_users(42))
    for e in _take(user_events(42, get_mix("baseline")), 2_000):
        assert e.user in users  # type: ignore[attr-defined]
        if e.action in ("product", "checkout"):  # type: ignore[attr-defined]
            assert e.target in SKUS  # type: ignore[attr-defined]


def test_streams_are_fixed_rate_and_seeded() -> None:
    mix = Mix("probe", 1.0, BASELINE_WEIGHTS, streams=(Stream("catalogue_nonce", 4.0),))
    events = _take(stream_events(42, mix, mix.streams[0]), 8)
    assert [e.at for e in events] == pytest.approx([0.25 * (i + 1) for i in range(8)])  # type: ignore[attr-defined]
    nonces = [e.target for e in events]  # type: ignore[attr-defined]
    assert len(set(nonces)) == 8
    assert nonces == [e.target for e in _take(stream_events(42, mix, mix.streams[0]), 8)]  # type: ignore[attr-defined]


def test_unknown_mix() -> None:
    with pytest.raises(KeyError, match="unknown traffic mix"):
        get_mix("nope")


# --- stats --------------------------------------------------------------------------------


def test_stats_window_rolls_and_totals_persist() -> None:
    stats = Stats()
    stats.record("browse", 10, error=False, declined=False)
    stats.record("checkout", 50, error=True, declined=False)
    stats.record("checkout", 30, error=False, declined=True)
    window = stats.roll()
    assert window["requests"] == 3
    assert window["errors"] == 1
    assert window["declined"] == 1
    assert stats.roll()["requests"] == 0
    assert stats.totals.summary()["requests"] == 3
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5
    assert percentile([], 0.5) is None


# --- deploy pipeline ----------------------------------------------------------------------


class FakeService:
    """A service that restarts onto whatever version the manifest says (or never does)."""

    def __init__(self, manifest: Path, version: str, *, comes_back: bool = True) -> None:
        self.manifest = manifest
        self.version = version
        self.comes_back = comes_back
        self.restarts: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ops/restart":
            self.restarts.append(request.headers.get("x-ops-token"))
            if self.comes_back:
                self.version = read_manifest(self.manifest)["services"]["user-service"]["version"]
            return httpx.Response(202)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": self.version})
        return httpx.Response(404)


async def test_deploy_writes_release_restarts_and_records(tmp_path: Path) -> None:
    manifest = tmp_path / "releases.json"
    events = tmp_path / "deploy-events.jsonl"
    service = FakeService(manifest, "2.3.0")
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))

    result = await deploy(
        "user-service",
        "2.4.0",
        config={"DATABASE_NAME": "users_v2"},
        manifest_path=manifest,
        ops_token="tok",
        sink=EventSink(events, None),
        service_url="http://user-service:8000",
        wait_s=2,
        poll_s=0.01,
        client=client,
    )

    assert result.status == "succeeded"
    assert result.previous_version == "2.3.0"
    assert service.restarts == ["tok"]
    assert read_manifest(manifest)["services"]["user-service"] == {
        "version": "2.4.0",
        "config": {"DATABASE_NAME": "users_v2"},
    }
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    assert [ln["event"] for ln in lines] == ["deployment started", "deployment finished"]
    assert lines[1]["status"] == "succeeded"
    assert lines[0]["deployment_id"] == lines[1]["deployment_id"]
    assert lines[0]["config_keys"] == ["DATABASE_NAME"]  # keys only, never values


async def test_deploy_that_never_becomes_healthy_is_failed(tmp_path: Path) -> None:
    manifest = tmp_path / "releases.json"
    service = FakeService(manifest, "2.3.0", comes_back=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    result = await deploy(
        "user-service",
        "2.4.0",
        manifest_path=manifest,
        ops_token="tok",
        sink=EventSink(tmp_path / "e.jsonl", None),
        service_url="http://user-service:8000",
        wait_s=0.2,
        poll_s=0.01,
        client=client,
    )
    assert result.status == "failed"
    # The release is recorded even though the rollout failed: rollback = deploy the old one.
    assert read_manifest(manifest)["services"]["user-service"]["version"] == "2.4.0"


async def test_deploy_rejects_unknown_service(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown service"):
        await deploy(
            "billing",
            "1.0.0",
            manifest_path=tmp_path / "m.json",
            ops_token="t",
            sink=EventSink(tmp_path / "e.jsonl", None),
        )
