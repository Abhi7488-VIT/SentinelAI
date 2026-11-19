"""Errors that map to HTTP responses, shared by all services."""

from __future__ import annotations


class ShopError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class NotFoundError(ShopError):
    status_code = 404
    code = "not_found"


class UnauthorizedError(ShopError):
    status_code = 401
    code = "unauthorized"


class PaymentDeclinedError(ShopError):
    status_code = 402
    code = "payment_declined"


class ServiceUnavailableError(ShopError):
    status_code = 503
    code = "service_unavailable"


class UpstreamError(ShopError):
    """A downstream call failed; status_code is what we return to our caller."""

    status_code = 502
    code = "bad_gateway"
