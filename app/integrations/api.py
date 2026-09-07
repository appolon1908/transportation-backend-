from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db, get_ingress_db
from app.integrations.models import (
    IntegrationConnection,
    IntegrationDelivery,
    IntegrationInboxMessage,
    IntegrationProvenanceEntry,
    IntegrationWebhookKey,
)
from app.integrations.security import canonical_json_bytes, resolve_secret, validate_destination_url
from app.integrations.service import (
    accept_inbound_webhook,
    append_provenance,
    audit,
    row_dict,
    set_tenant_context,
    utcnow,
)
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1", tags=["integrations"])
_ALLOWED_KINDS = {"ODOO_JSON2", "N8N_WEBHOOK", "SIGNED_WEBHOOK"}


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    kind: str
    base_url: str = Field(min_length=8, max_length=1000)
    endpoint_path: str | None = Field(default=None, max_length=500)
    secret_ref: str | None = Field(default=None, max_length=300)
    signing_secret_ref: str | None = Field(default=None, max_length=300)
    signing_key_id: str | None = Field(default=None, max_length=120)
    capability_code: str = Field(min_length=3, max_length=160)
    event_types: list[str] = Field(min_length=1, max_length=100)
    configuration: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    max_attempts: int = Field(default=8, ge=1, le=25)
    verify_tls: bool = True

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in _ALLOWED_KINDS:
            raise ValueError("Unsupported integration kind")
        return normalized

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, values: list[str]) -> list[str]:
        normalized = sorted({value.strip() for value in values if value.strip()})
        if not normalized:
            raise ValueError("At least one event type is required")
        if any(len(value) > 180 for value in normalized):
            raise ValueError("Event type is too long")
        return normalized


class ConnectionPatch(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=180)
    base_url: str | None = Field(default=None, min_length=8, max_length=1000)
    endpoint_path: str | None = Field(default=None, max_length=500)
    secret_ref: str | None = Field(default=None, max_length=300)
    signing_secret_ref: str | None = Field(default=None, max_length=300)
    signing_key_id: str | None = Field(default=None, max_length=120)
    capability_code: str | None = Field(default=None, min_length=3, max_length=160)
    event_types: list[str] | None = Field(default=None, max_length=100)
    configuration: dict[str, Any] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    max_attempts: int | None = Field(default=None, ge=1, le=25)
    verify_tls: bool | None = None
    enabled: bool | None = None


class WebhookKeyCreate(BaseModel):
    key_id: str = Field(min_length=1, max_length=120)
    secret_ref: str = Field(min_length=5, max_length=300)
    not_before: datetime | None = None
    expires_at: datetime | None = None
    revoke_other_keys: bool = False


class ReplayRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _secret_ref_shape(value: str | None) -> None:
    if value is not None and not value.startswith("env:"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SECRET_REFERENCE_INVALID",
                "message": "Only env:NAME secret references are accepted by this runtime.",
            },
        )


def safe_connection(item: IntegrationConnection) -> dict[str, Any]:
    result = row_dict(item)
    result.pop("secret_ref", None)
    result.pop("signing_secret_ref", None)
    result["credential_configured"] = bool(item.secret_ref)
    result["signing_secret_configured"] = bool(item.signing_secret_ref)
    return result


def safe_webhook_key(item: IntegrationWebhookKey) -> dict[str, Any]:
    result = row_dict(item)
    result.pop("secret_ref", None)
    result["secret_configured"] = True
    return result


async def tenant_connection(
    db: AsyncSession, connection_id: UUID, actor: Actor, *, for_update: bool = False
) -> IntegrationConnection:
    query = select(IntegrationConnection).where(
        IntegrationConnection.id == connection_id,
        IntegrationConnection.tenant_id == actor.tenant_id,
    )
    if for_update:
        query = query.with_for_update()
    item = await db.scalar(query)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Integration not found."})
    return item


@router.get("/admin/integrations")
async def list_integrations(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
) -> list[dict[str, Any]]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    items = (
        await db.scalars(
            select(IntegrationConnection)
            .where(IntegrationConnection.tenant_id == actor.tenant_id)
            .order_by(IntegrationConnection.name)
        )
    ).all()
    return [safe_connection(item) for item in items]


@router.post("/admin/integrations", status_code=201)
async def create_integration(
    payload: ConnectionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("admin.users.manage")
    _secret_ref_shape(payload.secret_ref)
    _secret_ref_shape(payload.signing_secret_ref)
    try:
        validate_destination_url(payload.base_url, payload.endpoint_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": str(exc), "message": "Integration destination is not allowed."},
        ) from exc
    if payload.kind == "ODOO_JSON2" and not payload.secret_ref:
        raise HTTPException(status_code=422, detail={"code": "ODOO_SECRET_REFERENCE_REQUIRED", "message": "Odoo bearer secret reference is required."})
    if payload.kind in {"N8N_WEBHOOK", "SIGNED_WEBHOOK"} and not payload.signing_secret_ref:
        raise HTTPException(status_code=422, detail={"code": "SIGNING_SECRET_REFERENCE_REQUIRED", "message": "Signing secret reference is required."})
    await set_tenant_context(db, actor.tenant_id, actor.subject)

    async def action():
        item = IntegrationConnection(
            tenant_id=actor.tenant_id,
            name=payload.name.strip(),
            kind=payload.kind,
            webhook_slug=uuid4().hex,
            base_url=payload.base_url.rstrip("/"),
            endpoint_path=payload.endpoint_path,
            secret_ref=payload.secret_ref,
            signing_secret_ref=payload.signing_secret_ref,
            signing_key_id=payload.signing_key_id,
            capability_code=payload.capability_code,
            event_types=payload.event_types,
            configuration=payload.configuration,
            timeout_seconds=payload.timeout_seconds,
            max_attempts=payload.max_attempts,
            verify_tls=payload.verify_tls,
            enabled=False,
        )
        db.add(item)
        await db.flush()
        return safe_connection(item), "IntegrationConnection", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="integration.connection.create",
        payload={**payload.model_dump(mode="json"), "secret_ref": "[REFERENCE]", "signing_secret_ref": "[REFERENCE]"},
        action=action,
        event_type="integration.connection.created.v1",
        audit_action="INTEGRATION_CONNECTION_CREATED",
    )


@router.get("/admin/integrations/{connection_id}")
async def get_integration(
    connection_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    item = await tenant_connection(db, connection_id, actor)
    response.headers["ETag"] = f'"{item.version}"'
    return safe_connection(item)


@router.patch("/admin/integrations/{connection_id}")
async def patch_integration(
    connection_id: UUID,
    payload: ConnectionPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("admin.users.manage")
    _secret_ref_shape(payload.secret_ref)
    _secret_ref_shape(payload.signing_secret_ref)
    await set_tenant_context(db, actor.tenant_id, actor.subject)

    async def action():
        item = await tenant_connection(db, connection_id, actor, for_update=True)
        if item.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={"code": "STALE_VERSION", "message": "Integration version is stale.", "current_version": item.version},
            )
        changes = payload.model_dump(exclude_unset=True)
        changes.pop("expected_version", None)
        candidate_base = str(changes.get("base_url", item.base_url))
        candidate_path = changes.get("endpoint_path", item.endpoint_path)
        try:
            validate_destination_url(candidate_base, candidate_path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": str(exc), "message": "Integration destination is not allowed."}) from exc
        for field_name, value in changes.items():
            if field_name == "event_types" and value is not None:
                value = sorted({str(item).strip() for item in value if str(item).strip()})
                if not value:
                    raise HTTPException(status_code=422, detail={"code": "EVENT_FILTER_REQUIRED", "message": "At least one event type is required."})
            setattr(item, field_name, value)
        item.version += 1
        return safe_connection(item), "IntegrationConnection", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="integration.connection.update",
        payload={"id": str(connection_id), **payload.model_dump(mode="json", exclude_none=True)},
        action=action,
        event_type="integration.connection.updated.v1",
        audit_action="INTEGRATION_CONNECTION_UPDATED",
    )


@router.post("/admin/integrations/{connection_id}/validate")
async def validate_integration(
    connection_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("admin.users.manage")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    item = await tenant_connection(db, connection_id, actor)
    problems: list[str] = []
    try:
        validate_destination_url(item.base_url, item.endpoint_path)
    except ValueError as exc:
        problems.append(str(exc))
    for secret_ref in [item.secret_ref, item.signing_secret_ref]:
        if secret_ref:
            try:
                resolve_secret(secret_ref)
            except RuntimeError as exc:
                problems.append(str(exc))
    if not item.event_types:
        problems.append("EVENT_FILTER_REQUIRED")
    return {"valid": not problems, "dry_run": True, "problems": sorted(set(problems))}


@router.get("/admin/integrations/{connection_id}/webhook-keys")
async def list_webhook_keys(
    connection_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> list[dict[str, Any]]:
    actor.require("admin.users.manage")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    await tenant_connection(db, connection_id, actor)
    items = (
        await db.scalars(
            select(IntegrationWebhookKey)
            .where(
                IntegrationWebhookKey.connection_id == connection_id,
                IntegrationWebhookKey.tenant_id == actor.tenant_id,
            )
            .order_by(IntegrationWebhookKey.created_at.desc())
        )
    ).all()
    return [safe_webhook_key(item) for item in items]


@router.post("/admin/integrations/{connection_id}/webhook-keys", status_code=201)
async def register_webhook_key(
    connection_id: UUID,
    payload: WebhookKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("admin.users.manage")
    _secret_ref_shape(payload.secret_ref)
    if payload.not_before and payload.expires_at and payload.expires_at <= payload.not_before:
        raise HTTPException(status_code=422, detail={"code": "WEBHOOK_KEY_WINDOW_INVALID", "message": "Webhook key expiry must follow activation."})
    await set_tenant_context(db, actor.tenant_id, actor.subject)

    async def action():
        connection = await tenant_connection(db, connection_id, actor, for_update=True)
        if payload.revoke_other_keys:
            existing = (
                await db.scalars(
                    select(IntegrationWebhookKey).where(
                        IntegrationWebhookKey.connection_id == connection.id,
                        IntegrationWebhookKey.tenant_id == actor.tenant_id,
                        IntegrationWebhookKey.active.is_(True),
                    )
                )
            ).all()
            for key in existing:
                key.active = False
                key.revoked_at = utcnow()
        item = IntegrationWebhookKey(
            tenant_id=actor.tenant_id,
            connection_id=connection.id,
            key_id=payload.key_id,
            secret_ref=payload.secret_ref,
            active=True,
            not_before=payload.not_before,
            expires_at=payload.expires_at,
        )
        db.add(item)
        await db.flush()
        connection.version += 1
        return safe_webhook_key(item), "IntegrationWebhookKey", item.id, 1

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="integration.webhook_key.register",
        payload={"connection_id": str(connection_id), "key_id": payload.key_id, "secret_ref": "[REFERENCE]"},
        action=action,
        event_type="integration.webhook_key.registered.v1",
        audit_action="INTEGRATION_WEBHOOK_KEY_REGISTERED",
    )


@router.get("/admin/integrations/{connection_id}/deliveries")
async def list_deliveries(
    connection_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> list[dict[str, Any]]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    await tenant_connection(db, connection_id, actor)
    query = select(IntegrationDelivery).where(
        IntegrationDelivery.tenant_id == actor.tenant_id,
        IntegrationDelivery.connection_id == connection_id,
    )
    if status_filter:
        query = query.where(IntegrationDelivery.status == status_filter.upper())
    items = (await db.scalars(query.order_by(IntegrationDelivery.created_at.desc()).limit(limit))).all()
    return [row_dict(item) for item in items]


@router.post("/admin/integrations/deliveries/{delivery_id}/replay")
async def replay_delivery(
    delivery_id: UUID,
    payload: ReplayRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    item = await db.scalar(
        select(IntegrationDelivery)
        .where(IntegrationDelivery.id == delivery_id, IntegrationDelivery.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Delivery not found."})
    if item.status not in {"FAILED_TERMINAL", "CANCELLED"}:
        raise HTTPException(status_code=409, detail={"code": "DELIVERY_NOT_REPLAYABLE", "message": "Only terminal deliveries may be replayed."})
    item.status = "RETRY"
    item.next_attempt_at = utcnow()
    item.claim_token = None
    item.claim_expires_at = None
    item.last_error_code = None
    item.last_error_detail = None
    correlation_id = getattr(request.state, "correlation_id", uuid4().hex)
    await audit(
        db,
        tenant_id=actor.tenant_id,
        actor_id=actor.subject,
        action="INTEGRATION_DELIVERY_REPLAYED",
        resource_type="IntegrationDelivery",
        resource_id=item.id,
        correlation_id=correlation_id,
        metadata={"reason": payload.reason},
    )
    await append_provenance(
        db,
        tenant_id=actor.tenant_id,
        chain_scope=f"outbound:{item.connection_id}",
        payload_hash=item.payload_hash,
        event_type="delivery.replayed",
        entity_type="IntegrationDelivery",
        entity_id=item.id,
        correlation_id=correlation_id,
        metadata={"reason": payload.reason, "actor": actor.subject},
    )
    await db.commit()
    return row_dict(item)


@router.get("/admin/integrations/inbox/messages")
async def list_inbox_messages(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> list[dict[str, Any]]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    query = select(IntegrationInboxMessage).where(IntegrationInboxMessage.tenant_id == actor.tenant_id)
    if status_filter:
        query = query.where(IntegrationInboxMessage.status == status_filter.upper())
    items = (await db.scalars(query.order_by(IntegrationInboxMessage.received_at.desc()).limit(limit))).all()
    return [row_dict(item) for item in items]


@router.post("/admin/integrations/inbox/{inbox_id}/replay")
async def replay_inbox_message(
    inbox_id: UUID,
    payload: ReplayRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    item = await db.scalar(
        select(IntegrationInboxMessage)
        .where(IntegrationInboxMessage.id == inbox_id, IntegrationInboxMessage.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Inbox message not found."})
    if item.status not in {"FAILED_TERMINAL", "PROCESSED"}:
        raise HTTPException(status_code=409, detail={"code": "INBOX_NOT_REPLAYABLE", "message": "Inbox message is not replayable."})
    item.status = "RETRY"
    item.next_attempt_at = utcnow()
    item.claim_token = None
    item.claim_expires_at = None
    item.processed_at = None
    correlation_id = getattr(request.state, "correlation_id", uuid4().hex)
    await audit(
        db,
        tenant_id=actor.tenant_id,
        actor_id=actor.subject,
        action="INTEGRATION_INBOX_REPLAYED",
        resource_type="IntegrationInboxMessage",
        resource_id=item.id,
        correlation_id=correlation_id,
        metadata={"reason": payload.reason},
    )
    await append_provenance(
        db,
        tenant_id=actor.tenant_id,
        chain_scope=f"inbound:{item.connection_id}",
        payload_hash=item.raw_hash,
        event_type="inbox.replayed",
        entity_type="IntegrationInboxMessage",
        entity_id=item.id,
        correlation_id=correlation_id,
        metadata={"reason": payload.reason, "actor": actor.subject},
    )
    await db.commit()
    return row_dict(item)


@router.get("/admin/integrations/provenance")
async def list_provenance(
    chain_scope: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> list[dict[str, Any]]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    query = select(IntegrationProvenanceEntry).where(
        IntegrationProvenanceEntry.tenant_id == actor.tenant_id
    )
    if chain_scope:
        query = query.where(IntegrationProvenanceEntry.chain_scope == chain_scope)
    items = (
        await db.scalars(
            query.order_by(
                IntegrationProvenanceEntry.chain_scope,
                IntegrationProvenanceEntry.sequence.desc(),
            ).limit(limit)
        )
    ).all()
    return [row_dict(item) for item in items]


@router.get("/admin/integrations/provenance/verify")
async def verify_provenance_chain(
    chain_scope: str,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)
    entries = (
        await db.scalars(
            select(IntegrationProvenanceEntry)
            .where(
                IntegrationProvenanceEntry.tenant_id == actor.tenant_id,
                IntegrationProvenanceEntry.chain_scope == chain_scope,
            )
            .order_by(IntegrationProvenanceEntry.sequence)
        )
    ).all()
    previous_hash: str | None = None
    expected_sequence = 1
    for entry in entries:
        material = canonical_json_bytes(
            {
                "tenant_id": str(entry.tenant_id),
                "chain_scope": entry.chain_scope,
                "sequence": entry.sequence,
                "previous_hash": entry.previous_hash,
                "payload_hash": entry.payload_hash,
                "event_type": entry.event_type,
                "entity_type": entry.entity_type,
                "entity_id": str(entry.entity_id),
                "correlation_id": entry.correlation_id,
                "created_at": entry.created_at.isoformat(),
                "metadata": entry.metadata_json,
            }
        )
        expected_hash = hashlib.sha256((previous_hash or "").encode("ascii") + material).hexdigest()
        if (
            entry.sequence != expected_sequence
            or entry.previous_hash != previous_hash
            or entry.entry_hash != expected_hash
        ):
            return {"valid": False, "checked": expected_sequence - 1, "failed_entry_id": str(entry.id)}
        previous_hash = entry.entry_hash
        expected_sequence += 1
    return {"valid": True, "checked": len(entries), "head_hash": previous_hash}


async def _lookup_public_connection(
    db: AsyncSession, webhook_slug: str
) -> IntegrationConnection:
    resolved = (
        await db.execute(
            text(
                "SELECT connection_id, tenant_id "
                "FROM public.freight_resolve_webhook_connection(:webhook_slug)"
            ),
            {"webhook_slug": webhook_slug},
        )
    ).one_or_none()
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "WEBHOOK_NOT_FOUND",
                "message": "Webhook endpoint not found.",
            },
        )
    connection_id, tenant_id = resolved
    await set_tenant_context(db, tenant_id, "webhook-ingress")
    item = await db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == connection_id,
            IntegrationConnection.tenant_id == tenant_id,
            IntegrationConnection.enabled.is_(True),
        )
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "WEBHOOK_NOT_FOUND",
                "message": "Webhook endpoint not found.",
            },
        )
    return item


async def _receive_webhook(
    *,
    webhook_slug: str,
    provider: str,
    request: Request,
    x_webhook_id: str,
    x_webhook_timestamp: str,
    x_webhook_key_id: str,
    x_webhook_signature: str,
    x_event_type: str | None,
    db: AsyncSession,
) -> dict[str, Any]:
    connection = await _lookup_public_connection(db, webhook_slug)
    inbox, duplicate = await accept_inbound_webhook(
        db,
        connection=connection,
        provider=provider,
        request=request,
        external_event_id=x_webhook_id,
        event_type_header=x_event_type,
        key_id=x_webhook_key_id,
        timestamp_header=x_webhook_timestamp,
        signature=x_webhook_signature,
    )
    return {"accepted": True, "duplicate": duplicate, "inbox_id": str(inbox.id)}


@router.post("/integrations/{webhook_slug}/webhooks/{provider}", status_code=202)
async def receive_webhook(
    webhook_slug: str,
    provider: str,
    request: Request,
    x_webhook_id: str = Header(alias="X-Webhook-Id", min_length=1, max_length=240),
    x_webhook_timestamp: str = Header(alias="X-Webhook-Timestamp"),
    x_webhook_key_id: str = Header(alias="X-Webhook-Key-Id", min_length=1, max_length=120),
    x_webhook_signature: str = Header(alias="X-Webhook-Signature", min_length=16, max_length=300),
    x_event_type: str | None = Header(default=None, alias="X-Event-Type", max_length=180),
    db: AsyncSession = Depends(get_ingress_db),
) -> dict[str, Any]:
    return await _receive_webhook(
        webhook_slug=webhook_slug,
        provider=provider,
        request=request,
        x_webhook_id=x_webhook_id,
        x_webhook_timestamp=x_webhook_timestamp,
        x_webhook_key_id=x_webhook_key_id,
        x_webhook_signature=x_webhook_signature,
        x_event_type=x_event_type,
        db=db,
    )


@router.post("/integrations/tracking/{provider}/webhooks", status_code=202)
async def receive_compatibility_tracking_webhook(
    provider: str,
    request: Request,
    x_integration_slug: str = Header(alias="X-Integration-Slug", min_length=16, max_length=120),
    x_webhook_id: str = Header(alias="X-Webhook-Id", min_length=1, max_length=240),
    x_webhook_timestamp: str = Header(alias="X-Webhook-Timestamp"),
    x_webhook_key_id: str = Header(alias="X-Webhook-Key-Id", min_length=1, max_length=120),
    x_webhook_signature: str = Header(alias="X-Webhook-Signature", min_length=16, max_length=300),
    x_event_type: str | None = Header(default=None, alias="X-Event-Type", max_length=180),
    db: AsyncSession = Depends(get_ingress_db),
) -> dict[str, Any]:
    return await _receive_webhook(
        webhook_slug=x_integration_slug,
        provider=provider,
        request=request,
        x_webhook_id=x_webhook_id,
        x_webhook_timestamp=x_webhook_timestamp,
        x_webhook_key_id=x_webhook_key_id,
        x_webhook_signature=x_webhook_signature,
        x_event_type=x_event_type,
        db=db,
    )
