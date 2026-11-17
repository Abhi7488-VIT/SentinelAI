"""Custom column types."""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class IPAddress(TypeDecorator[str]):
    """Postgres INET exposed as `str` in Python (asyncpg would return ipaddress objects)."""

    impl = INET
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else str(value)
