"""Application error types and the centralized exception handlers.

Every non-2xx response has the shape defined by `app.schemas.common.ErrorResponse`:
    {"error": {"code", "message", "request_id", "details"?}}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    message: str = "Internal server error."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.code = code or type(self).code
        self.headers = dict(headers) if headers else None
        super().__init__(self.message)


class NotAuthenticatedError(AppError):
    status_code = 401
    code = "not_authenticated"
    message = "Authentication required."


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state."


class InvalidRequestError(AppError):
    """Well-formed input that is semantically invalid (e.g. a dependency on itself)."""

    status_code = 422
    code = "invalid_request"
    message = "The request is invalid."


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Try again later."

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(headers={"Retry-After": str(max(1, retry_after_seconds))})


class DependencyUnavailableError(AppError):
    status_code = 503
    code = "dependency_unavailable"
    message = "A required dependency is unavailable."


_HTTP_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def error_json_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    details: Sequence[Mapping[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    if request_id is None:
        # Local import: request_context imports this module.
        from app.core.request_context import current_request_id

        request_id = current_request_id()
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        body["details"] = list(details)
    return JSONResponse({"error": body}, status_code=status_code, headers=headers)


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    # Deliberately excludes "input"/"ctx": they can echo request bodies (passwords).
    return [
        {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", "")), "type": err.get("type")}
        for err in exc.errors()
    ]


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    err = cast(AppError, exc)  # registered for AppError only
    return error_json_response(err.status_code, err.code, err.message, headers=err.headers)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    details = _validation_details(cast(RequestValidationError, exc))
    return error_json_response(
        422, "validation_error", "Request validation failed.", details=details
    )


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    code = _HTTP_STATUS_CODES.get(http_exc.status_code, "http_error")
    message = http_exc.detail if isinstance(http_exc.detail, str) else "HTTP error."
    return error_json_response(http_exc.status_code, code, message, headers=http_exc.headers)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    # Unhandled exceptions are converted in RequestContextMiddleware (see its docstring).
