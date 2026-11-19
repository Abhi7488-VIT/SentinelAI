"""api-gateway: the shop's public API. Authenticates sessions and routes to services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shop.api_gateway.cache import ResponseCache
from shop.common.app import create_service_app
from shop.common.downstream import Downstream
from shop.common.errors import UnauthorizedError, UpstreamError
from shop.common.settings import ServiceSettings


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class CheckoutItem(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=1, le=10)


class CheckoutIn(BaseModel):
    items: list[CheckoutItem] = Field(min_length=1, max_length=10)
    shipping_address: str = Field(min_length=1, max_length=300)


def users_api(request: Request) -> Downstream:
    api: Downstream = request.app.state.users
    return api


def orders_api(request: Request) -> Downstream:
    api: Downstream = request.app.state.orders
    return api


UsersDep = Annotated[Downstream, Depends(users_api)]
OrdersDep = Annotated[Downstream, Depends(orders_api)]


async def current_user(
    users: UsersDep, authorization: Annotated[str | None, Header()] = None
) -> int:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Sign in required.")
    try:
        session = await users.get_json(f"/sessions/{token}")
    except UpstreamError as exc:
        if exc.status_code == 404:
            raise UnauthorizedError("Session expired. Sign in again.") from exc
        raise
    return int(session["user_id"])


UserId = Annotated[int, Depends(current_user)]


def create_app() -> FastAPI:
    settings = ServiceSettings.for_service("api-gateway")
    if settings.user_service_url is None or settings.order_service_url is None:
        raise RuntimeError("USER_SERVICE_URL and ORDER_SERVICE_URL are required")
    user_url, order_url = settings.user_service_url, settings.order_service_url

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.users = Downstream("user-service", user_url, settings.http_timeout_s)
        app.state.orders = Downstream("order-service", order_url, settings.http_timeout_s)
        app.state.catalogue_cache = ResponseCache(ttl_s=30.0)
        yield
        await app.state.users.close()
        await app.state.orders.close()

    app = create_service_app(settings, lifespan)

    @app.get("/api/products")
    async def products(
        request: Request,
        orders: OrdersDep,
        category: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 24,
    ) -> JSONResponse:
        cache: ResponseCache = request.app.state.catalogue_cache
        key = str(request.url)
        payload = cache.get(key)
        if payload is None:
            params: dict[str, Any] = {"limit": limit}
            if category:
                params["category"] = category
            payload = await orders.get_json("/products", params=params)
            cache.put(key, payload)
        return JSONResponse(payload)

    @app.get("/api/products/{sku}")
    async def product(sku: str, orders: OrdersDep) -> Any:
        return await orders.get_json(f"/products/{sku}")

    @app.post("/api/login")
    async def login(body: LoginIn, users: UsersDep) -> Any:
        return await users.post_json("/login", body.model_dump())

    @app.get("/api/account")
    async def account(user_id: UserId, users: UsersDep, orders: OrdersDep) -> Any:
        user = await users.get_json(f"/users/{user_id}")
        history = await orders.get_json("/orders", params={"user_id": user_id, "limit": 20})
        return {"user": user, "orders": history}

    @app.post("/api/orders", status_code=201)
    async def checkout(body: CheckoutIn, user_id: UserId, orders: OrdersDep) -> Any:
        return await orders.post_json("/orders", {"user_id": user_id, **body.model_dump()})

    return app
