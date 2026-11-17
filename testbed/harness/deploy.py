"""The testbed's deploy pipeline: release a version of a shop service and record the deploy.

    python -m harness.deploy SERVICE VERSION [--config KEY=VALUE ...] [--wait 60]

1. Writes the release (version + config) to the releases manifest the services read at start.
2. Asks the service to restart (POST /ops/restart). If the service is not answering (e.g. it is
   crash-looping), Docker's restart policy still brings it up on the new manifest.
3. Waits until the service reports the new version on /healthz (succeeded) or times out (failed).
4. Emits deploy events like a CI/CD system would: OTLP log records from service
   `deploy-pipeline` (event.name=deployment) and a line in runs/deploy-events.jsonl.
   Events carry what a pipeline knows (service, versions, outcome) — nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

SERVICES = ("frontend", "api-gateway", "user-service", "order-service", "payment-service")

logger = logging.getLogger("deploy-pipeline")


@dataclass(frozen=True)
class DeployEvent:
    event: Literal["deployment started", "deployment finished"]
    deployment_id: str
    service: str
    version: str
    previous_version: str | None
    status: Literal["in_progress", "succeeded", "failed"]
    timestamp: str
    config_keys: tuple[str, ...] = ()


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    data.setdefault("services", {})
    return data


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: a restarting service never reads half a file


class EventSink:
    """Deploy events to OTLP (if configured) and an append-only JSONL file."""

    def __init__(self, events_file: Path, otlp_endpoint: str | None) -> None:
        self.events_file = events_file
        self._provider: Any = None
        self._handler: logging.Handler | None = None
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.instrumentation.logging.handler import LoggingHandler
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.resources import Resource

            self._provider = LoggerProvider(
                resource=Resource.create(
                    {"service.name": "deploy-pipeline", "service.namespace": "cicd"}
                )
            )
            self._provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/logs")
                )
            )
            self._handler = LoggingHandler(level=logging.INFO, logger_provider=self._provider)
            logger.addHandler(self._handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False

    def emit(self, event: DeployEvent) -> None:
        record = asdict(event)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        with self.events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        logger.info(
            event.event,
            extra={
                "event.name": "deployment",
                "deployment.id": event.deployment_id,
                "deployment.service.name": event.service,
                "deployment.version": event.version,
                "deployment.previous_version": event.previous_version or "",
                "deployment.status": event.status,
            },
        )

    def close(self) -> None:
        if self._provider is not None:
            self._provider.force_flush()
            self._provider.shutdown()


@dataclass(frozen=True)
class DeployResult:
    deployment_id: str
    service: str
    version: str
    previous_version: str | None
    status: Literal["succeeded", "failed"]
    seconds: float


async def _running_version(client: httpx.AsyncClient, base_url: str) -> str | None:
    try:
        resp = await client.get(f"{base_url}/healthz", timeout=2)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    version = resp.json().get("version")
    return str(version) if version else None


async def deploy(
    service: str,
    version: str,
    *,
    config: dict[str, str] | None = None,
    manifest_path: Path,
    ops_token: str,
    sink: EventSink,
    service_url: str | None = None,
    wait_s: float = 60.0,
    poll_s: float = 1.0,
    client: httpx.AsyncClient | None = None,
) -> DeployResult:
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}")
    base_url = service_url or f"http://{service}:8000"
    deployment_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    manifest = read_manifest(manifest_path)

    async with client or httpx.AsyncClient() as client:
        previous = (manifest["services"].get(service) or {}).get("version") or (
            await _running_version(client, base_url)
        )
        sink.emit(
            DeployEvent(
                event="deployment started",
                deployment_id=deployment_id,
                service=service,
                version=version,
                previous_version=previous,
                status="in_progress",
                timestamp=datetime.now(UTC).isoformat(),
                config_keys=tuple(sorted(config or {})),
            )
        )
        manifest["services"][service] = {"version": version, "config": dict(config or {})}
        write_manifest(manifest_path, manifest)
        # Not answering (e.g. crash-looping): the restart policy starts it on the new release.
        with contextlib.suppress(httpx.HTTPError):
            await client.post(
                f"{base_url}/ops/restart", headers={"X-Ops-Token": ops_token}, timeout=5
            )

        status: Literal["succeeded", "failed"] = "failed"
        deadline = time.monotonic() + wait_s
        await asyncio.sleep(poll_s)
        while time.monotonic() < deadline:
            if await _running_version(client, base_url) == version:
                status = "succeeded"
                break
            await asyncio.sleep(poll_s)

    sink.emit(
        DeployEvent(
            event="deployment finished",
            deployment_id=deployment_id,
            service=service,
            version=version,
            previous_version=previous,
            status=status,
            timestamp=datetime.now(UTC).isoformat(),
            config_keys=tuple(sorted(config or {})),
        )
    )
    return DeployResult(
        deployment_id=deployment_id,
        service=service,
        version=version,
        previous_version=previous,
        status=status,
        seconds=round(time.monotonic() - started, 1),
    )


def _parse_config(pairs: list[str]) -> dict[str, str]:
    config: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--config expects KEY=VALUE, got {pair!r}")
        config[key] = value
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.deploy", description=__doc__.splitlines()[0])
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("version")
    parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for health")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    sink = EventSink(
        Path(os.environ.get("DEPLOY_EVENTS_FILE", "/runs/deploy-events.jsonl")),
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    try:
        result = asyncio.run(
            deploy(
                args.service,
                args.version,
                config=_parse_config(args.config),
                manifest_path=Path(os.environ.get("RELEASES_FILE", "/releases/releases.json")),
                ops_token=os.environ["OPS_TOKEN"],
                sink=sink,
                wait_s=args.wait,
            )
        )
    finally:
        sink.close()
    sys.stdout.write(json.dumps(asdict(result)) + "\n")
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
