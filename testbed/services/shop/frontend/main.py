"""frontend: server-rendered storefront pages. Every page is built from api-gateway calls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from shop.common.app import create_service_app
from shop.common.downstream import Downstream
from shop.common.errors import UpstreamError
from shop.common.settings import ServiceSettings

SESSION_COOKIE = "shop_session"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _gateway(request: Request) -> Downstream:
    gw: Downstream = request.app.state.gateway
    return gw


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def create_app() -> FastAPI:
    settings = ServiceSettings.for_service("frontend")
    if settings.gateway_url is None:
        raise RuntimeError("GATEWAY_URL is required")
    gateway_url = settings.gateway_url

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.gateway = Downstream("api-gateway", gateway_url, settings.http_timeout_s)
        yield
        await app.state.gateway.close()

    app = create_service_app(settings, lifespan)

    def page(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    @app.exception_handler(UpstreamError)
    async def _upstream(request: Request, exc: UpstreamError) -> HTMLResponse:
        return page(request, "error.html", status=exc.status_code, message=exc.message)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        products = await _gateway(request).get_json("/api/products", params={"limit": 12})
        return page(request, "products.html", title="Featured", products=products)

    @app.get("/products", response_class=HTMLResponse)
    async def catalogue(request: Request) -> HTMLResponse:
        # Query parameters (category, limit, client hints) are passed through to the API.
        params = dict(request.query_params)
        products = await _gateway(request).get_json("/api/products", params=params)
        title = params.get("category", "All products").title()
        return page(request, "products.html", title=title, products=products)

    @app.get("/products/{sku}", response_class=HTMLResponse)
    async def product(request: Request, sku: str) -> HTMLResponse:
        item = await _gateway(request).get_json(f"/api/products/{sku}")
        return page(request, "product.html", product=item)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> HTMLResponse:
        return page(request, "login.html")

    @app.post("/login")
    async def login(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        try:
            session = await _gateway(request).post_json(
                "/api/login", {"email": email, "password": password}
            )
        except UpstreamError as exc:
            if exc.status_code == 401:
                return page(request, "login.html", status=401, error="Invalid email or password.")
            raise
        resp = RedirectResponse("/account", status_code=303)
        resp.set_cookie(SESSION_COOKIE, session["token"], httponly=True, samesite="lax")
        return resp

    @app.get("/account", response_class=HTMLResponse)
    async def account(
        request: Request, shop_session: Annotated[str | None, Cookie()] = None
    ) -> Response:
        try:
            data = await _gateway(request).get_json("/api/account", headers=_auth(shop_session))
        except UpstreamError as exc:
            if exc.status_code == 401:
                return RedirectResponse("/login", status_code=303)
            raise
        return page(request, "account.html", user=data["user"], orders=data["orders"])

    @app.post("/checkout")
    async def checkout(
        request: Request,
        sku: Annotated[str, Form()],
        qty: Annotated[int, Form()] = 1,
        shop_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        try:
            order = await _gateway(request).post_json(
                "/api/orders",
                {"items": [{"sku": sku, "qty": qty}], "shipping_address": "On file"},
                headers=_auth(shop_session),
            )
        except UpstreamError as exc:
            if exc.status_code == 401:
                return RedirectResponse("/login", status_code=303)
            if exc.status_code == 402:
                return page(request, "error.html", status=402, message=exc.message)
            raise
        return page(request, "order.html", status=201, order=order)

    return app
