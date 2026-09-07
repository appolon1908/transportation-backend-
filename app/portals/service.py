from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, TypeVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Capability, Document, Load
from app.portals.models import PortalAccessAudit, PortalPrincipalBinding
from app.security import Actor

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def actor_issuer(actor: Actor) -> str:
    value = getattr(actor, "issuer", None)
    return str(value or os.getenv("OIDC_ISSUER", "unknown-issuer")).rstrip("/")


def has_column(model: type, name: str) -> bool:
    return name in model.__table__.columns


def column(model: type, name: str):
    if not has_column(model, name):
        raise RuntimeError(f"{model.__name__} does not expose required column {name}.")
    return getattr(model, name)


def serialize_columns(item: object, allowed: Iterable[str] | None = None) -> dict[str, Any]:
    allowed_set = set(allowed) if allowed is not None else None
    result: dict[str, Any] = {}
    for table_column in item.__table__.columns:  # type: ignore[attr-defined]
        if allowed_set is None or table_column.name in allowed_set:
            result[table_column.name] = getattr(item, table_column.name)
    return result


def encode_cursor(item: object) -> str | None:
    if not hasattr(item, "id"):
        return None
    value = {
        "created_at": getattr(item, "created_at", None).isoformat()
        if getattr(item, "created_at", None)
        else None,
        "id": str(getattr(item, "id")),
    }
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime | None, UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = (
            datetime.fromisoformat(str(decoded["created_at"]).replace("Z", "+00:00"))
            if decoded.get("created_at")
            else None
        )
        return created_at, UUID(str(decoded["id"]))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CURSOR", "message": "Pagination cursor is invalid."},
        ) from exc


def apply_keyset(statement: Select, model: type, cursor: str | None) -> Select:
    if not cursor:
        return statement
    created_at, object_id = decode_cursor(cursor)
    if created_at is not None and has_column(model, "created_at"):
        return statement.where(
            or_(
                column(model, "created_at") < created_at,
                and_(column(model, "created_at") == created_at, column(model, "id") < object_id),
            )
        )
    return statement.where(column(model, "id") < object_id)


def ordered(statement: Select, model: type) -> Select:
    if has_column(model, "created_at"):
        return statement.order_by(column(model, "created_at").desc(), column(model, "id").desc())
    return statement.order_by(column(model, "id").desc())


async def page_rows(
    db: AsyncSession,
    *,
    statement: Select,
    model: type[T],
    limit: int,
    cursor: str | None,
) -> tuple[list[T], str | None]:
    bounded = max(1, min(limit, 200))
    statement = apply_keyset(statement, model, cursor)
    rows = (await db.scalars(ordered(statement, model).limit(bounded + 1))).all()
    has_more = len(rows) > bounded
    selected = list(rows[:bounded])
    next_cursor = encode_cursor(selected[-1]) if has_more and selected else None
    return selected, next_cursor


async def capability_is_enabled(db: AsyncSession, tenant_id: UUID, code: str) -> bool:
    item = await db.scalar(
        select(Capability).where(Capability.tenant_id == tenant_id, Capability.code == code)
    )
    return bool(item and item.enabled)


async def record_access(
    db: AsyncSession,
    *,
    actor: Actor,
    portal_kind: str,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    allowed: bool,
    reason_code: str,
    correlation_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        PortalAccessAudit(
            tenant_id=actor.tenant_id,
            principal_issuer=actor_issuer(actor),
            principal_subject=actor.subject,
            portal_kind=portal_kind,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            allowed=allowed,
            reason_code=reason_code,
            correlation_id=correlation_id,
            metadata_json=metadata or {},
        )
    )


async def require_portal_binding(
    db: AsyncSession,
    *,
    actor: Actor,
    portal_kind: str,
    capability_code: str,
    action: str,
    correlation_id: str,
) -> PortalPrincipalBinding:
    required_permission = {
        "CUSTOMER": "portal.customer",
        "CARRIER": "portal.carrier",
    }.get(portal_kind)
    if required_permission:
        actor.require(required_permission)

    if not await capability_is_enabled(db, actor.tenant_id, capability_code):
        await record_access(
            db,
            actor=actor,
            portal_kind=portal_kind,
            action=action,
            resource_type="PortalPrincipalBinding",
            resource_id=None,
            allowed=False,
            reason_code="PORTAL_CAPABILITY_DISABLED",
            correlation_id=correlation_id,
        )
        await db.commit()
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PORTAL_CAPABILITY_DISABLED",
                "message": f"{portal_kind.title()} portal access is not enabled for this organization.",
            },
        )

    binding = await db.scalar(
        select(PortalPrincipalBinding).where(
            PortalPrincipalBinding.tenant_id == actor.tenant_id,
            PortalPrincipalBinding.principal_issuer == actor_issuer(actor),
            PortalPrincipalBinding.principal_subject == actor.subject,
            PortalPrincipalBinding.portal_kind == portal_kind,
            PortalPrincipalBinding.status == "ACTIVE",
        )
    )
    if binding is None:
        await record_access(
            db,
            actor=actor,
            portal_kind=portal_kind,
            action=action,
            resource_type="PortalPrincipalBinding",
            resource_id=None,
            allowed=False,
            reason_code="PORTAL_PRINCIPAL_NOT_BOUND",
            correlation_id=correlation_id,
        )
        await db.commit()
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PORTAL_PRINCIPAL_NOT_BOUND",
                "message": "This identity is not bound to a portal account.",
            },
        )

    await record_access(
        db,
        actor=actor,
        portal_kind=portal_kind,
        action=action,
        resource_type=portal_kind.title(),
        resource_id=binding.resource_id,
        allowed=True,
        reason_code="PORTAL_ACCESS_ALLOWED",
        correlation_id=correlation_id,
    )
    return binding


async def require_bound_resource(
    db: AsyncSession,
    *,
    model: type[T],
    actor: Actor,
    binding: PortalPrincipalBinding,
    resource_id: UUID,
    ownership_column: str,
    resource_type: str,
    action: str,
    correlation_id: str,
) -> T:
    statement = select(model).where(
        column(model, "tenant_id") == actor.tenant_id,
        column(model, "id") == resource_id,
        column(model, ownership_column) == binding.resource_id,
    )
    item = await db.scalar(statement)
    if item is None:
        await record_access(
            db,
            actor=actor,
            portal_kind=binding.portal_kind,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            allowed=False,
            reason_code="PORTAL_RESOURCE_NOT_FOUND",
            correlation_id=correlation_id,
        )
        await db.commit()
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": f"{resource_type} not found."},
        )
    return item


async def carrier_load_ids(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    carrier_id: UUID,
) -> set[UUID]:
    if not has_column(Load, "carrier_id"):
        return set()
    return set(
        await db.scalars(
            select(column(Load, "id")).where(
                column(Load, "tenant_id") == tenant_id,
                column(Load, "carrier_id") == carrier_id,
            )
        )
    )


async def documents_for_resources(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    resource_ids: Sequence[UUID],
    resource_types: Sequence[str],
    limit: int,
) -> list[Document]:
    if not resource_ids:
        return []
    bounded = max(1, min(limit, 200))

    if {"resource_id", "resource_type"}.issubset(Document.__table__.columns.keys()):
        statement = select(Document).where(
            column(Document, "tenant_id") == tenant_id,
            column(Document, "resource_id").in_(resource_ids),
            column(Document, "resource_type").in_(resource_types),
        )
    elif has_column(Document, "load_id"):
        statement = select(Document).where(
            column(Document, "tenant_id") == tenant_id,
            column(Document, "load_id").in_(resource_ids),
        )
    else:
        return []

    if has_column(Document, "created_at"):
        statement = statement.order_by(column(Document, "created_at").desc())
    else:
        statement = statement.order_by(column(Document, "id").desc())
    return list((await db.scalars(statement.limit(bounded))).all())
