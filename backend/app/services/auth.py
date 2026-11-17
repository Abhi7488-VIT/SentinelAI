"""Authentication use cases. See ADR 0001 for the token model.

Transactions: each public method commits exactly once. Failed logins and refresh-token
reuse commit their audit/revocation rows *before* raising, so the evidence is kept.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, ConflictError
from app.core.logging import get_logger
from app.core.metrics import AUTH_EVENTS
from app.core.rate_limit import RateLimiter, hashed_identifier
from app.core.security.passwords import (
    hash_password,
    needs_rehash,
    verify_against_dummy,
    verify_password,
)
from app.core.security.tokens import (
    AccessToken,
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from app.models.organization import Organization, OrganizationMember
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.organizations import MembershipRepository
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.audit import AuditService, RequestMeta
from app.services.orgs import OrgService

logger = get_logger("app.auth")


class InvalidCredentialsError(AppError):
    status_code = 401
    code = "invalid_credentials"
    message = "Invalid email or password."


class RefreshTokenInvalidError(AppError):
    status_code = 401
    code = "refresh_token_invalid"
    message = "Refresh token is invalid or expired. Please sign in again."


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access: AccessToken
    refresh_token: str
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user: User
    organization: Organization
    tokens: IssuedTokens


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User
    tokens: IssuedTokens


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings, limiter: RateLimiter) -> None:
        self.session = session
        self.settings = settings
        self.limiter = limiter
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)
        self.members = MembershipRepository(session)
        self.audit = AuditService(session)

    # ---- rate limiting -------------------------------------------------------------------

    async def _limit(self, scope: str, identifier: str) -> None:
        await self.limiter.hit(
            scope,
            identifier,
            limit=self.settings.auth_rate_limit_attempts,
            window_seconds=self.settings.auth_rate_limit_window_seconds,
        )

    # ---- use cases -----------------------------------------------------------------------

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        org_name: str,
        meta: RequestMeta,
    ) -> RegistrationResult:
        await self._limit("register:ip", meta.ip or "unknown")
        email = normalize_email(email)
        password_hash = await hash_password(password)
        now = datetime.now(UTC)
        try:
            user = await self.users.add(
                User(
                    email=email,
                    password_hash=password_hash,
                    display_name=display_name,
                    last_login_at=now,
                )
            )
            org = await OrgService(self.session).create_org(
                name=org_name, slug=None, owner_id=user.id, meta=meta
            )
            tokens = await self._issue_new_family(user.id, meta, now)
            self.audit.record(
                "auth.register", meta, actor_user_id=user.id, target_type="user", target_id=user.id
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _conflict_from_integrity(exc) from exc
        AUTH_EVENTS.labels(action="register", outcome="success").inc()
        logger.info("auth.registered", user_id=str(user.id), org_id=str(org.id))
        return RegistrationResult(user=user, organization=org, tokens=tokens)

    async def login(self, *, email: str, password: str, meta: RequestMeta) -> LoginResult:
        email = normalize_email(email)
        await self._limit("login:ip", meta.ip or "unknown")
        await self._limit("login:email", hashed_identifier(email))

        user = await self.users.get_by_email(email)
        if user is None:
            await verify_against_dummy(password)
            await self._fail_login(meta, actor=None, reason="unknown_email")
        elif not await verify_password(user.password_hash, password):
            await self._fail_login(meta, actor=user.id, reason="bad_password")
        elif not user.is_active:
            await self._fail_login(meta, actor=user.id, reason="inactive")

        now = datetime.now(UTC)
        if needs_rehash(user.password_hash):
            user.password_hash = await hash_password(password)
        user.last_login_at = now
        tokens = await self._issue_new_family(user.id, meta, now)
        self.audit.record(
            "auth.login.succeeded",
            meta,
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
        )
        await self.session.commit()
        AUTH_EVENTS.labels(action="login", outcome="success").inc()
        return LoginResult(user=user, tokens=tokens)

    async def refresh(self, *, raw_token: str | None, meta: RequestMeta) -> IssuedTokens:
        await self._limit("refresh:ip", meta.ip or "unknown")
        if not raw_token:
            AUTH_EVENTS.labels(action="refresh", outcome="missing").inc()
            raise RefreshTokenInvalidError()

        now = datetime.now(UTC)
        current = await self.tokens.get_by_hash_for_update(hash_refresh_token(raw_token))
        if current is None:
            AUTH_EVENTS.labels(action="refresh", outcome="unknown").inc()
            raise RefreshTokenInvalidError()

        if current.used_at is not None or current.revoked_at is not None:
            # A rotated/revoked token was presented again: assume theft, kill the session.
            revoked = await self.tokens.revoke_family(
                current.family_id, reason="reuse_detected", now=now
            )
            self.audit.record(
                "auth.refresh.reuse_detected",
                meta,
                actor_user_id=current.user_id,
                target_type="session",
                target_id=current.family_id,
                metadata={"revoked_tokens": revoked},
            )
            await self.session.commit()
            AUTH_EVENTS.labels(action="refresh", outcome="reuse_detected").inc()
            logger.warning("auth.refresh.reuse_detected", user_id=str(current.user_id))
            raise RefreshTokenInvalidError()

        if current.expires_at <= now or current.family_expires_at <= now:
            AUTH_EVENTS.labels(action="refresh", outcome="expired").inc()
            raise RefreshTokenInvalidError()

        user = await self.users.get(current.user_id)
        if user is None or not user.is_active:
            await self.tokens.revoke_family(current.family_id, reason="admin", now=now)
            await self.session.commit()
            raise RefreshTokenInvalidError()

        new_row, raw_new = await self._new_refresh_row(
            user.id, current.family_id, current.family_expires_at, meta, now
        )
        current.used_at = now
        current.revoked_at = now
        current.revoked_reason = "rotated"
        current.replaced_by_id = new_row.id
        self.audit.record(
            "auth.refresh.succeeded",
            meta,
            actor_user_id=user.id,
            target_type="session",
            target_id=current.family_id,
        )
        await self.session.commit()
        AUTH_EVENTS.labels(action="refresh", outcome="success").inc()
        return IssuedTokens(
            access=create_access_token(user.id, self.settings, now=now),
            refresh_token=raw_new,
            refresh_expires_at=new_row.expires_at,
        )

    async def logout(self, *, raw_token: str | None, meta: RequestMeta) -> None:
        """Idempotent: unknown or already-revoked tokens are a no-op."""
        if not raw_token:
            return
        now = datetime.now(UTC)
        current = await self.tokens.get_by_hash_for_update(hash_refresh_token(raw_token))
        if current is None or current.revoked_at is not None:
            await self.session.rollback()
            return
        await self.tokens.revoke_family(current.family_id, reason="logout", now=now)
        self.audit.record(
            "auth.logout",
            meta,
            actor_user_id=current.user_id,
            target_type="session",
            target_id=current.family_id,
        )
        await self.session.commit()
        AUTH_EVENTS.labels(action="logout", outcome="success").inc()

    async def memberships(
        self, user_id: uuid.UUID
    ) -> list[tuple[Organization, OrganizationMember]]:
        return await self.members.list_orgs_for_user(user_id)

    # ---- helpers -------------------------------------------------------------------------

    async def _fail_login(
        self, meta: RequestMeta, *, actor: uuid.UUID | None, reason: str
    ) -> NoReturn:
        self.audit.record(
            "auth.login.failed", meta, actor_user_id=actor, metadata={"reason": reason}
        )
        await self.session.commit()
        AUTH_EVENTS.labels(action="login", outcome="failure").inc()
        raise InvalidCredentialsError()

    async def _issue_new_family(
        self, user_id: uuid.UUID, meta: RequestMeta, now: datetime
    ) -> IssuedTokens:
        family_expires_at = now + timedelta(seconds=self.settings.refresh_family_ttl_seconds)
        row, raw = await self._new_refresh_row(user_id, uuid.uuid4(), family_expires_at, meta, now)
        return IssuedTokens(
            access=create_access_token(user_id, self.settings, now=now),
            refresh_token=raw,
            refresh_expires_at=row.expires_at,
        )

    async def _new_refresh_row(
        self,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        family_expires_at: datetime,
        meta: RequestMeta,
        now: datetime,
    ) -> tuple[RefreshToken, str]:
        raw = generate_refresh_token()
        expires_at = min(
            now + timedelta(seconds=self.settings.refresh_token_ttl_seconds), family_expires_at
        )
        row = await self.tokens.add(
            RefreshToken(
                user_id=user_id,
                family_id=family_id,
                token_hash=hash_refresh_token(raw),
                expires_at=expires_at,
                family_expires_at=family_expires_at,
                user_agent=meta.user_agent,
                ip=meta.ip,
            )
        )
        return row, raw


def _conflict_from_integrity(exc: IntegrityError) -> AppError:
    text = str(exc.orig)
    if "uq_users_email" in text:
        return ConflictError("An account with this email already exists.", code="email_taken")
    if "uq_organizations_slug" in text:
        return ConflictError("Organization slug is already taken.", code="slug_taken")
    return ConflictError()
