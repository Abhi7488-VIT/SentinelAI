"""Auth routes (thin): validation, cookie handling, response mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import (
    AuthServiceDep,
    CurrentUser,
    RequestMetaDep,
    SettingsDep,
    verify_origin,
)
from app.core.config import Settings
from app.models.organization import Role
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MembershipOut,
    MeResponse,
    OrganizationOut,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserOut,
)
from app.schemas.common import ErrorResponse
from app.services.auth import IssuedTokens

REFRESH_COOKIE = "sentinel_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

router = APIRouter(prefix="/auth", tags=["auth"])

_errors: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _set_refresh_cookie(response: Response, tokens: IssuedTokens, settings: Settings) -> None:
    max_age = int((tokens.refresh_expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=max(0, max_age),
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    responses=_errors,
)
async def register(
    body: RegisterRequest,
    response: Response,
    service: AuthServiceDep,
    meta: RequestMetaDep,
    settings: SettingsDep,
) -> RegisterResponse:
    result = await service.register(
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        org_name=body.org_name,
        meta=meta,
    )
    _set_refresh_cookie(response, result.tokens, settings)
    return RegisterResponse(
        access_token=result.tokens.access.token,
        expires_in=result.tokens.access.expires_in,
        user=UserOut.model_validate(result.user),
        organization=OrganizationOut.model_validate(result.organization),
        membership_role=Role.OWNER,
    )


@router.post("/login", response_model=LoginResponse, responses=_errors)
async def login(
    body: LoginRequest,
    response: Response,
    service: AuthServiceDep,
    meta: RequestMetaDep,
    settings: SettingsDep,
) -> LoginResponse:
    result = await service.login(email=body.email, password=body.password, meta=meta)
    _set_refresh_cookie(response, result.tokens, settings)
    return LoginResponse(
        access_token=result.tokens.access.token,
        expires_in=result.tokens.access.expires_in,
        user=UserOut.model_validate(result.user),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={**_errors, 403: {"model": ErrorResponse}},
    dependencies=[Depends(verify_origin)],
)
async def refresh(
    request: Request,
    response: Response,
    service: AuthServiceDep,
    meta: RequestMetaDep,
    settings: SettingsDep,
) -> TokenResponse:
    tokens = await service.refresh(raw_token=request.cookies.get(REFRESH_COOKIE), meta=meta)
    _set_refresh_cookie(response, tokens, settings)
    return TokenResponse(access_token=tokens.access.token, expires_in=tokens.access.expires_in)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={403: {"model": ErrorResponse}},
    dependencies=[Depends(verify_origin)],
)
async def logout(
    request: Request,
    service: AuthServiceDep,
    meta: RequestMetaDep,
    settings: SettingsDep,
) -> Response:
    await service.logout(raw_token=request.cookies.get(REFRESH_COOKIE), meta=meta)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response, settings)
    return response


@router.get("/me", response_model=MeResponse, responses={401: {"model": ErrorResponse}})
async def me(user: CurrentUser, service: AuthServiceDep) -> MeResponse:
    memberships = await service.memberships(user.id)
    return MeResponse(
        user=UserOut.model_validate(user),
        memberships=[
            MembershipOut(org_id=org.id, org_name=org.name, org_slug=org.slug, role=Role(m.role))
            for org, m in memberships
        ],
    )
