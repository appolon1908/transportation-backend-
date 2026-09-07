from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db, set_session_context
from app.platform.models import (
    ExternalIdentity,
    Membership,
    MembershipRole,
    Permission,
    Principal,
    Role,
    RolePermission,
)


@dataclass(frozen=True)
class Actor:
    subject: str
    issuer: str
    principal_id: UUID
    membership_id: UUID
    tenant_id: UUID
    organization_id: UUID | None
    customer_id: UUID | None
    carrier_id: UUID | None
    principal_type: str
    permissions: frozenset[str]
    roles: frozenset[str]

    def require(self, permission: str) -> None:
        if permission not in self.permissions and "*" not in self.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "Permission denied."},
            )

    def require_customer_scope(self, customer_id: UUID) -> None:
        if self.customer_id is not None and self.customer_id != customer_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "Resource not found."},
            )

    def require_carrier_scope(self, carrier_id: UUID) -> None:
        if self.carrier_id is not None and self.carrier_id != carrier_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "Resource not found."},
            )


@dataclass
class _JwksCacheEntry:
    keys: dict[str, dict[str, Any]]
    expires_at: float
    stale_until: float


_jwks_cache: dict[str, _JwksCacheEntry] = {}
_jwks_locks: dict[str, asyncio.Lock] = {}


def _auth_error(code: str, message: str, http_status: int = 401) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


async def _load_jwks(settings: Settings) -> dict[str, dict[str, Any]]:
    url = settings.resolved_oidc_jwks_url
    now = time.monotonic()
    cached = _jwks_cache.get(url)
    if cached and cached.expires_at > now:
        return cached.keys

    lock = _jwks_locks.setdefault(url, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _jwks_cache.get(url)
        if cached and cached.expires_at > now:
            return cached.keys
        try:
            async with httpx.AsyncClient(
                timeout=settings.oidc_http_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
            raw_keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(raw_keys, list) or not raw_keys:
                raise ValueError("JWKS response does not contain keys")
            keys = {
                str(key["kid"]): key
                for key in raw_keys
                if isinstance(key, dict) and key.get("kid")
            }
            if not keys:
                raise ValueError("JWKS response has no keyed signing keys")
            _jwks_cache[url] = _JwksCacheEntry(
                keys=keys,
                expires_at=now + settings.oidc_jwks_cache_seconds,
                stale_until=(
                    now
                    + settings.oidc_jwks_cache_seconds
                    + settings.oidc_jwks_stale_seconds
                ),
            )
            return keys
        except Exception as exc:
            if cached and cached.stale_until > now:
                return cached.keys
            raise _auth_error(
                "IDENTITY_PROVIDER_UNAVAILABLE",
                "Identity signing keys are unavailable.",
                503,
            ) from exc


async def _validated_claims(token: str, settings: Settings) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        kid = str(header.get("kid") or "")
        algorithm = str(header.get("alg") or "")
    except jwt.PyJWTError as exc:
        raise _auth_error("INVALID_TOKEN", "Token header is invalid.") from exc
    if not kid:
        raise _auth_error("INVALID_TOKEN", "Token signing key id is missing.")
    if algorithm not in settings.oidc_algorithm_list:
        raise _auth_error("INVALID_TOKEN", "Token algorithm is not allowed.")

    keys = await _load_jwks(settings)
    raw_key = keys.get(kid)
    if raw_key is None:
        _jwks_cache.pop(settings.resolved_oidc_jwks_url, None)
        keys = await _load_jwks(settings)
        raw_key = keys.get(kid)
    if raw_key is None:
        raise _auth_error("INVALID_TOKEN", "Token signing key is unknown.")

    try:
        signing_key = jwt.PyJWK.from_dict(raw_key, algorithm=algorithm).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=settings.oidc_algorithm_list,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=settings.oidc_clock_skew_seconds,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise _auth_error("INVALID_TOKEN", "Token validation failed.") from exc

    allowed_azp = settings.oidc_allowed_azp_list
    if allowed_azp and str(claims.get("azp") or "") not in allowed_azp:
        raise _auth_error("INVALID_TOKEN", "Token client is not allowed.")
    return dict(claims)


async def _load_local_actor(
    db: AsyncSession,
    claims: dict[str, Any],
    selected_tenant: str | None,
    settings: Settings,
) -> Actor:
    subject = str(claims["sub"])
    issuer = str(claims.get("iss") or settings.oidc_issuer).rstrip("/")
    tenant_raw = selected_tenant or claims.get("tenant_id")
    if not tenant_raw:
        raise _auth_error(
            "TENANT_SELECTION_REQUIRED",
            "Select a tenant using X-Tenant-Id.",
            409,
        )
    try:
        tenant_id = UUID(str(tenant_raw))
    except ValueError as exc:
        raise _auth_error("INVALID_TENANT", "Tenant selection is invalid.") from exc

    identity_row = (
        await db.execute(
            select(ExternalIdentity, Principal)
            .join(Principal, Principal.id == ExternalIdentity.principal_id)
            .where(
                ExternalIdentity.issuer == issuer,
                ExternalIdentity.subject == subject,
                ExternalIdentity.enabled.is_(True),
            )
        )
    ).one_or_none()
    if identity_row is None:
        raise _auth_error(
            "IDENTITY_NOT_BOUND",
            "The authenticated identity is not registered locally.",
            403,
        )
    identity, principal = identity_row
    if principal.status != "ACTIVE":
        raise _auth_error("PRINCIPAL_DISABLED", "The local principal is disabled.", 403)

    membership = await db.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.principal_id == principal.id,
            Membership.status == "ACTIVE",
        )
    )
    if membership is None:
        raise _auth_error(
            "TENANT_MEMBERSHIP_REQUIRED",
            "No active membership exists for the selected tenant.",
            403,
        )

    role_rows = (
        await db.execute(
            select(Role.code, Permission.code)
            .select_from(MembershipRole)
            .join(Role, Role.id == MembershipRole.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .where(
                MembershipRole.membership_id == membership.id,
                Role.tenant_id == tenant_id,
            )
        )
    ).all()
    roles = frozenset(role_code for role_code, _ in role_rows)
    permissions = frozenset(
        permission_code
        for _, permission_code in role_rows
        if permission_code is not None
    )
    identity.last_seen_at = datetime.now(timezone.utc)

    return Actor(
        subject=subject,
        issuer=issuer,
        principal_id=principal.id,
        membership_id=membership.id,
        tenant_id=tenant_id,
        organization_id=membership.organization_id,
        customer_id=membership.customer_id,
        carrier_id=membership.carrier_id,
        principal_type=principal.principal_type,
        permissions=permissions,
        roles=roles,
    )


async def get_actor(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_dev_tenant_id: str | None = Header(default=None, alias="X-Dev-Tenant-Id"),
    x_dev_actor: str | None = Header(default=None, alias="X-Dev-Actor"),
    x_dev_permissions: str | None = Header(default=None, alias="X-Dev-Permissions"),
) -> Actor:
    settings = get_settings()

    if (
        not authorization
        and settings.allow_development_identity_headers
        and not settings.is_production
    ):
        if not x_dev_tenant_id:
            raise _auth_error(
                "AUTH_REQUIRED",
                "Bearer token or development tenant header is required.",
            )
        try:
            tenant_id = UUID(x_dev_tenant_id)
        except ValueError as exc:
            raise _auth_error("INVALID_TENANT", "Invalid development tenant.") from exc
        subject = x_dev_actor or "developer"
        permissions = frozenset(
            value.strip()
            for value in (x_dev_permissions or "*").split(",")
            if value.strip()
        )
        actor = Actor(
            subject=subject,
            issuer="development",
            principal_id=uuid5(NAMESPACE_URL, f"principal:{subject}"),
            membership_id=uuid5(NAMESPACE_URL, f"membership:{tenant_id}:{subject}"),
            tenant_id=tenant_id,
            organization_id=None,
            customer_id=None,
            carrier_id=None,
            principal_type="USER",
            permissions=permissions,
            roles=frozenset({"development"}),
        )
        await set_session_context(db, tenant_id, subject)
        request.state.actor = actor
        return actor

    if not authorization or not authorization.startswith("Bearer "):
        raise _auth_error("AUTH_REQUIRED", "Bearer token is required.")
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise _auth_error(
            "IDENTITY_NOT_CONFIGURED",
            "OIDC is not configured.",
            503,
        )

    token = authorization.removeprefix("Bearer ").strip()
    claims = await _validated_claims(token, settings)
    actor = await _load_local_actor(db, claims, x_tenant_id, settings)
    await set_session_context(db, actor.tenant_id, str(actor.principal_id))
    request.state.actor = actor
    return actor
