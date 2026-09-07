from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db
from app.integrations.provenance import append_provenance
from app.models import Carrier, Customer
from app.portals.models import PortalAccessAudit, PortalPrincipalBinding
from app.portals.schemas import BindingIn, BindingPatch
from app.portals.service import page_rows, serialize_columns
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1/admin/portal-bindings", tags=["portal-admin"])


def _trusted_issuers() -> set[str]:
    return {
        item.strip().rstrip("/")
        for item in os.getenv(
            "TRUSTED_OIDC_ISSUERS",
            os.getenv("OIDC_ISSUER", ""),
        ).split(",")
        if item.strip()
    }


async def _validate_resource(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    portal_kind: str,
    resource_id: UUID,
) -> None:
    model = Customer if portal_kind == "CUSTOMER" else Carrier
    item = await db.scalar(
        select(model).where(model.tenant_id == tenant_id, model.id == resource_id)
    )
    if item is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PORTAL_RESOURCE_NOT_FOUND",
                "message": f"The {portal_kind.lower()} account does not exist in this organization.",
            },
        )


def _validate_binding_payload(payload: BindingIn | BindingPatch) -> None:
    if len(json.dumps(payload.metadata, separators=(",", ":"), default=str).encode()) > 10_000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "METADATA_TOO_LARGE",
                "message": "Binding metadata exceeds 10 KB.",
            },
        )


@router.get("")
async def list_bindings(
    portal_kind: str | None = Query(default=None, pattern="^(CUSTOMER|CARRIER)$"),
    binding_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(ACTIVE|SUSPENDED|REVOKED)$",
    ),
    principal_subject: str | None = Query(default=None, max_length=220),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.read")
    statement = select(PortalPrincipalBinding).where(
        PortalPrincipalBinding.tenant_id == actor.tenant_id
    )
    if portal_kind:
        statement = statement.where(PortalPrincipalBinding.portal_kind == portal_kind)
    if binding_status:
        statement = statement.where(PortalPrincipalBinding.status == binding_status)
    if principal_subject:
        statement = statement.where(
            PortalPrincipalBinding.principal_subject == principal_subject
        )
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=PortalPrincipalBinding,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [serialize_columns(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.post("", status_code=201)
async def create_binding(
    payload: BindingIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")
    _validate_binding_payload(payload)
    issuer = payload.principal_issuer.rstrip("/")
    trusted = _trusted_issuers()
    if not trusted or issuer not in trusted:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNTRUSTED_PRINCIPAL_ISSUER",
                "message": "Principal issuer is not in TRUSTED_OIDC_ISSUERS.",
            },
        )
    await _validate_resource(
        db,
        tenant_id=actor.tenant_id,
        portal_kind=payload.portal_kind,
        resource_id=payload.resource_id,
    )

    async def action():
        existing = await db.scalar(
            select(PortalPrincipalBinding).where(
                PortalPrincipalBinding.tenant_id == actor.tenant_id,
                PortalPrincipalBinding.principal_issuer == issuer,
                PortalPrincipalBinding.principal_subject == payload.principal_subject,
                PortalPrincipalBinding.portal_kind == payload.portal_kind,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PORTAL_BINDING_EXISTS",
                    "message": "This principal already has a binding for the requested portal.",
                    "binding_id": str(existing.id),
                },
            )
        item = PortalPrincipalBinding(
            tenant_id=actor.tenant_id,
            principal_issuer=issuer,
            principal_subject=payload.principal_subject,
            portal_kind=payload.portal_kind,
            resource_id=payload.resource_id,
            display_label=payload.display_label,
            status=payload.status,
            metadata_json=payload.metadata,
            created_by=actor.subject,
        )
        db.add(item)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.bindings",
            event_type="portal.binding.created",
            entity_id=str(item.id),
            payload=serialize_columns(item),
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return serialize_columns(item), "PortalPrincipalBinding", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="portal.binding.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="portal.binding.created.v1",
        audit_action="PORTAL_BINDING_CREATED",
    )


@router.patch("/{binding_id}")
async def update_binding(
    binding_id: UUID,
    payload: BindingPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")
    _validate_binding_payload(payload)

    async def action():
        item = await db.scalar(
            select(PortalPrincipalBinding)
            .where(
                PortalPrincipalBinding.id == binding_id,
                PortalPrincipalBinding.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Portal binding not found."},
            )
        if item.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Portal binding version is stale.",
                    "current_version": item.version,
                },
            )
        item.display_label = payload.display_label
        item.status = payload.status
        item.metadata_json = payload.metadata
        item.version += 1
        if payload.status == "REVOKED":
            item.revoked_by = actor.subject
            item.revoked_at = datetime.now(timezone.utc)
        else:
            item.revoked_by = None
            item.revoked_at = None
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.bindings",
            event_type="portal.binding.updated",
            entity_id=str(item.id),
            payload=serialize_columns(item),
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return serialize_columns(item), "PortalPrincipalBinding", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="portal.binding.update",
        payload={"binding_id": str(binding_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="portal.binding.updated.v1",
        audit_action="PORTAL_BINDING_UPDATED",
    )


@router.get("/access-audit")
async def list_portal_access_audit(
    allowed: bool | None = Query(default=None),
    portal_kind: str | None = Query(default=None, pattern="^(CUSTOMER|CARRIER)$"),
    principal_subject: str | None = Query(default=None, max_length=220),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.audit.read")
    statement = select(PortalAccessAudit).where(
        PortalAccessAudit.tenant_id == actor.tenant_id
    )
    if allowed is not None:
        statement = statement.where(PortalAccessAudit.allowed == allowed)
    if portal_kind:
        statement = statement.where(PortalAccessAudit.portal_kind == portal_kind)
    if principal_subject:
        statement = statement.where(
            PortalAccessAudit.principal_subject == principal_subject
        )
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=PortalAccessAudit,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [serialize_columns(item) for item in items],
        "next_cursor": next_cursor,
    }
