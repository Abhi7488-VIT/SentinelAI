"""user-service: Users, login and sessions."""

from __future__ import annotations

from fastapi import FastAPI

from shop.common.app import create_service_app
from shop.common.settings import ServiceSettings


def create_app() -> FastAPI:
    return create_service_app(ServiceSettings.for_service("user-service"))
