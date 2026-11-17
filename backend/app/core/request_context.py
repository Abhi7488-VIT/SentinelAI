"""Per-request context: request ID propagation, access logging, metrics, last-resort 500s.

Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) so contextvars flow into the
endpoint and streaming responses are not buffered. It is the outermost user middleware, so it
also turns unhandled exceptions into the uniform 500 error body while the request ID is still
bound (Starlette's ServerErrorMiddleware would run after our context is gone).
"""

from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_json_response
from app.core.logging import get_logger
from app.core.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

logger = get_logger("app.http")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def current_request_id() -> str | None:
    return request_id_var.get()


def _resolve_request_id(raw: str | None) -> str:
    if raw and _VALID_REQUEST_ID.match(raw):
        return raw
    return uuid.uuid4().hex


def _route_template(scope: Scope) -> str:
    """Low-cardinality path label for metrics (route template, never raw path)."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "<unmatched>"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token = request_id_var.set(request_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        response_started = False
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("http.unhandled_exception")
            if not response_started:
                response = error_json_response(
                    500, "internal_error", "Internal server error.", request_id=request_id
                )
                await response(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            route = _route_template(scope)
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
            HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(duration)
            if route not in {"/health", "/metrics"}:
                logger.info(
                    "http.request",
                    method=method,
                    path=scope.get("path", ""),
                    route=route,
                    status=status_code,
                    duration_ms=round(duration * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()
            request_id_var.reset(token)
