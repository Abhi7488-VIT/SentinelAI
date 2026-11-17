"""HTTP client for calling other shop services, with consistent failure mapping.

connection refused / DNS failure -> 503 (dependency unavailable)
timeout                          -> 504
downstream 4xx                   -> same status (the caller's request was the problem)
downstream 5xx                   -> 502
"""

from __future__ import annotations

from typing import Any

import httpx

from shop.common.errors import UpstreamError


class Downstream:
    def __init__(self, name: str, base_url: str, timeout_s: float) -> None:
        self.name = name
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(2.0, timeout_s)),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self.client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise UpstreamError(
                f"{self.name} is unavailable (connection failed).",
                status_code=503,
                code="dependency_unavailable",
            ) from exc
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                f"{self.name} timed out.", status_code=504, code="dependency_timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise UpstreamError(
                f"{self.name} connection error.", status_code=502, code="dependency_error"
            ) from exc
        if resp.status_code >= 500:
            raise UpstreamError(
                f"{self.name} returned {resp.status_code}.",
                status_code=502,
                code="dependency_error",
            )
        if resp.status_code >= 400:
            detail = _error_message(resp) or f"{self.name} returned {resp.status_code}."
            raise UpstreamError(detail, status_code=resp.status_code, code=_error_code(resp))
        return resp

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        return (await self.request("GET", path, **kwargs)).json()

    async def post_json(self, path: str, payload: Any, **kwargs: Any) -> Any:
        return (await self.request("POST", path, json=payload, **kwargs)).json()

    async def close(self) -> None:
        await self.client.aclose()


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _error_message(resp: httpx.Response) -> str | None:
    msg = _error_body(resp).get("message")
    return str(msg) if msg else None


def _error_code(resp: httpx.Response) -> str:
    return str(_error_body(resp).get("code") or "upstream_client_error")
