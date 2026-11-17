"""Shared pagination query parameters for every list endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query

from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int
    cursor: str | None


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> PageParams:
    return PageParams(limit=limit, cursor=cursor)


PageDep = Annotated[PageParams, Depends(page_params)]
