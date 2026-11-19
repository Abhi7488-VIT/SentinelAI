"""OTLP/HTTP receiver routes (thin). Served only by the ingest process (`app.ingest_main`).

The OTel Collector's `otlphttp` exporter appends `/v1/traces`, `/v1/logs` and `/v1/metrics` to its
endpoint, so the exporter endpoint is `http://<ingest>:8001/api/v1/otlp`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.ingest.batch import Signal
from app.ingest.decode import PayloadTooLargeError
from app.services.ingest import IngestService

router = APIRouter(prefix="/api/v1/otlp/v1", tags=["otlp"])


async def _read_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise PayloadTooLargeError()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:  # never buffer more than the cap, whatever the headers said
            raise PayloadTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


async def _handle(request: Request, signal: Signal) -> Response:
    service: IngestService = request.app.state.ingest_service
    body = await _read_body(request, service.settings.ingest_max_body_bytes)
    result = await service.ingest(
        signal,
        authorization=request.headers.get("authorization"),
        content_type=request.headers.get("content-type"),
        content_encoding=request.headers.get("content-encoding"),
        body=body,
    )
    return Response(content=result.body, media_type=result.media_type)


@router.post("/traces")
async def export_traces(request: Request) -> Response:
    return await _handle(request, "traces")


@router.post("/logs")
async def export_logs(request: Request) -> Response:
    return await _handle(request, "logs")


@router.post("/metrics")
async def export_metrics(request: Request) -> Response:
    return await _handle(request, "metrics")
