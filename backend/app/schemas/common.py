"""Shared API schemas: uniform error envelope and cursor pagination."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ErrorDetail(BaseModel):
    loc: list[str | int]
    msg: str
    type: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


class APIModel(BaseModel):
    """Base for response models built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)
