from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.adapters import DeliveryResult, deliver
from app.integrations.models import (
    IntegrationCommandRequest,
    IntegrationConnection,
    IntegrationDelivery,
    IntegrationDeliveryAttempt,
    IntegrationInboxMessage,
    IntegrationProvenanceEntry,
    IntegrationWebhookKey,
)
from app.integrations.security import (
    canonical_json_bytes,
    parse_signature_timestamp,
    redact_headers,
    resolve_secret,
    sha256_hex,
    verify_signature,
)
from app.models import (
    AuditEntry,
    Capability,
    Load,
    OperationalException,
    OutboxMessage,
    TrackingEvent,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def set_tenant_context(db: AsyncSession, tenant_id: UUID, actor_id: str = "integration-worker") -> None:
    await db.execute(
        text(
            "SELECT set_config('app.tenant_id', :tenant_id, true), "
            "set_config('app.actor_id', :actor_id, true)"
        ),
        {"tenant_id": str(tenant_id), "actor_id": actor_id},
    )


def row_dict(model: Any) -> dict[str, Any]:
    result = {column.name: getattr(model, column.name) for column in model.__table__.columns}
    for key, value in list(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def integration_event_matches(connection: IntegrationConnection, event_type: str) -> bool:
    filters = list(connection.event_types or [])
    if not filters:
        return False
    for pattern in filters:
        if pattern == "*" or pattern == event_type:
            return True
        if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
            return True
    return False


async def capability_enabled(db: AsyncSession, tenant_id: UUID, code: str | None) -> bool:
    if not code:
        return False
    configured = await db.scalar(
        select(Capability.enabled).where(Capability.tenant_id == tenant_id, Capability.code == code)
    )
    return configured is True


async def append_provenance(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    chain_scope: str,
    payload_hash: str,
    event_type: str,
    entity_type: str,
    entity_id: UUID,
    correlation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> IntegrationProvenanceEntry:
    lock_key = f"{tenant_id}:{chain_scope}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key})
    previous = await db.scalar(
        select(IntegrationProvenanceEntry)
        .where(
            IntegrationProvenanceEntry.tenant_id == tenant_id,
            IntegrationProvenanceEntry.chain_scope == chain_scope,
        )
        .order_by(IntegrationProvenanceEntry.sequence.desc())
        .limit(1)
    )
    sequence = (previous.sequence + 1) if previous else 1
    previous_hash = previous.entry_hash if previous else None
    created_at = utcnow()
    hash_material = canonical_json_bytes(
        {
            "tenant_id": str(tenant_id),
            "chain_scope": chain_scope,
            "sequence": sequence,
            "previous_hash": previous_hash,
            "payload_hash": payload_hash,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "correlation_id": correlation_id,
            "created_at": created_at.isoformat(),
            "metadata": metadata or {},
        }
    )
    entry_hash = hashlib.sha256((previous_hash or "").encode("ascii") + hash_material).hexdigest()
    entry = IntegrationProvenanceEntry(
        tenant_id=tenant_id,
        chain_scope=chain_scope,
        sequence=sequence,
        previous_hash=previous_hash,
        payload_hash=payload_hash,
        entry_hash=entry_hash,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        correlation_id=correlation_id,
        metadata_json=metadata or {},
        created_at=created_at,
    )
    db.add(entry)
    await db.flush()
    return entry


async def audit(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: UUID,
    correlation_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditEntry(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=correlation_id,
            metadata_json=metadata or {},
        )
    )


async def accept_inbound_webhook(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    provider: str,
    request: Request,
    external_event_id: str,
    event_type_header: str | None,
    key_id: str,
    timestamp_header: str,
    signature: str,
) -> tuple[IntegrationInboxMessage, bool]:
    max_body_bytes = int(os.getenv("INTEGRATION_MAX_WEBHOOK_BYTES", "1000000"))
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "WEBHOOK_TOO_LARGE", "message": "Webhook body exceeds the configured limit."},
        )
    raw_body = await request.body()
    if len(raw_body) > max_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "WEBHOOK_TOO_LARGE", "message": "Webhook body exceeds the configured limit."},
        )

    signature_time = parse_signature_timestamp(timestamp_header)
    now = utcnow()
    webhook_key = await db.scalar(
        select(IntegrationWebhookKey).where(
            IntegrationWebhookKey.connection_id == connection.id,
            IntegrationWebhookKey.tenant_id == connection.tenant_id,
            IntegrationWebhookKey.key_id == key_id,
            IntegrationWebhookKey.active.is_(True),
            or_(IntegrationWebhookKey.not_before.is_(None), IntegrationWebhookKey.not_before <= now),
            or_(IntegrationWebhookKey.expires_at.is_(None), IntegrationWebhookKey.expires_at > now),
        )
    )
    if webhook_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WEBHOOK_KEY_NOT_ACCEPTED", "message": "Webhook key is unknown or inactive."},
        )
    verify_signature(
        secret=resolve_secret(webhook_key.secret_ref),
        timestamp=timestamp_header,
        raw_body=raw_body,
        supplied=signature,
    )
    raw_hash = sha256_hex(raw_body)
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_WEBHOOK_JSON", "message": "Webhook body must be valid JSON."},
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_WEBHOOK_PAYLOAD", "message": "Webhook body must be a JSON object."},
        )
    event_type = event_type_header or str(payload.get("type") or "")
    if not event_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "WEBHOOK_EVENT_TYPE_REQUIRED", "message": "Webhook event type is required."},
        )

    existing = await db.scalar(
        select(IntegrationInboxMessage).where(
            IntegrationInboxMessage.connection_id == connection.id,
            IntegrationInboxMessage.external_event_id == external_event_id,
        )
    )
    if existing is not None:
        if existing.raw_hash != raw_hash:
            await append_provenance(
                db,
                tenant_id=connection.tenant_id,
                chain_scope=f"inbound:{connection.id}",
                payload_hash=raw_hash,
                event_type="webhook.id_payload_conflict",
                entity_type="IntegrationInboxMessage",
                entity_id=existing.id,
                metadata={"provider": provider, "external_event_id": external_event_id},
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "WEBHOOK_ID_PAYLOAD_CONFLICT",
                    "message": "This webhook event ID was already accepted with a different payload.",
                },
            )
        return existing, True

    inbox = IntegrationInboxMessage(
        tenant_id=connection.tenant_id,
        connection_id=connection.id,
        provider=provider[:120],
        external_event_id=external_event_id[:240],
        event_type=event_type[:180],
        key_id=key_id[:120],
        signature_verified=True,
        signature_timestamp=signature_time,
        raw_hash=raw_hash,
        payload=payload,
        headers_redacted=redact_headers(dict(request.headers)),
        status="PENDING",
    )
    db.add(inbox)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        await set_tenant_context(db, connection.tenant_id)
        raced = await db.scalar(
            select(IntegrationInboxMessage).where(
                IntegrationInboxMessage.connection_id == connection.id,
                IntegrationInboxMessage.external_event_id == external_event_id,
            )
        )
        if raced is None or raced.raw_hash != raw_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "WEBHOOK_DEDUPLICATION_CONFLICT", "message": "Webhook race conflict."},
            )
        return raced, True

    correlation_id = getattr(request.state, "correlation_id", external_event_id)
    await append_provenance(
        db,
        tenant_id=connection.tenant_id,
        chain_scope=f"inbound:{connection.id}",
        payload_hash=raw_hash,
        event_type="webhook.accepted",
        entity_type="IntegrationInboxMessage",
        entity_id=inbox.id,
        correlation_id=correlation_id,
        metadata={"provider": provider, "external_event_id": external_event_id, "key_id": key_id},
    )
    await db.commit()
    return inbox, False


async def fanout_outbox_batch(db: AsyncSession, *, limit: int = 100) -> int:
    outbox_items = (
        await db.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.status.in_(["PENDING_CONFIGURATION", "PENDING"]))
            .order_by(OutboxMessage.created_at)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).all()
    created = 0
    for outbox in outbox_items:
        await set_tenant_context(db, outbox.tenant_id)
        connections = (
            await db.scalars(
                select(IntegrationConnection).where(
                    IntegrationConnection.tenant_id == outbox.tenant_id,
                    IntegrationConnection.enabled.is_(True),
                )
            )
        ).all()
        selected = [c for c in connections if integration_event_matches(c, outbox.event_type)]
        if not selected:
            continue
        payload = dict(outbox.payload or {})
        payload.setdefault("schema_version", outbox.schema_version)
        payload.setdefault("correlation_id", outbox.correlation_id)
        payload.setdefault("aggregate_id", str(outbox.aggregate_id))
        payload.setdefault("aggregate_type", outbox.aggregate_type)
        payload_hash = sha256_hex(canonical_json_bytes(payload))
        for connection in selected:
            statement = (
                pg_insert(IntegrationDelivery)
                .values(
                    id=uuid4(),
                    tenant_id=outbox.tenant_id,
                    connection_id=connection.id,
                    outbox_id=outbox.id,
                    event_id=outbox.id,
                    event_type=outbox.event_type,
                    payload=payload,
                    payload_hash=payload_hash,
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=utcnow(),
                )
                .on_conflict_do_nothing(constraint="uq_integration_delivery_outbox")
                .returning(IntegrationDelivery.id)
            )
            delivery_id = await db.scalar(statement)
            if delivery_id:
                created += 1
                await append_provenance(
                    db,
                    tenant_id=outbox.tenant_id,
                    chain_scope=f"outbound:{connection.id}",
                    payload_hash=payload_hash,
                    event_type="delivery.created",
                    entity_type="IntegrationDelivery",
                    entity_id=delivery_id,
                    correlation_id=outbox.correlation_id,
                    metadata={"event_type": outbox.event_type, "connection_kind": connection.kind},
                )
        outbox.status = "FANOUT_COMPLETE"
    await db.commit()
    return created


@dataclass(frozen=True)
class ClaimedDelivery:
    delivery_id: UUID
    claim_token: UUID


async def claim_delivery_batch(db: AsyncSession, *, limit: int = 25) -> list[ClaimedDelivery]:
    now = utcnow()
    lease_seconds = int(os.getenv("INTEGRATION_DELIVERY_LEASE_SECONDS", "90"))
    items = (
        await db.scalars(
            select(IntegrationDelivery)
            .where(
                IntegrationDelivery.status.in_(["PENDING", "RETRY", "IN_FLIGHT"]),
                IntegrationDelivery.next_attempt_at <= now,
                or_(
                    IntegrationDelivery.status != "IN_FLIGHT",
                    IntegrationDelivery.claim_expires_at.is_(None),
                    IntegrationDelivery.claim_expires_at <= now,
                ),
            )
            .order_by(IntegrationDelivery.next_attempt_at, IntegrationDelivery.created_at)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).all()
    claimed: list[ClaimedDelivery] = []
    for item in items:
        token = uuid4()
        item.status = "IN_FLIGHT"
        item.claim_token = token
        item.claim_expires_at = now + timedelta(seconds=lease_seconds)
        item.attempts += 1
        claimed.append(ClaimedDelivery(item.id, token))
    await db.commit()
    return claimed


def retry_delay_seconds(attempt: int) -> int:
    base = int(os.getenv("INTEGRATION_RETRY_BASE_SECONDS", "10"))
    cap = int(os.getenv("INTEGRATION_RETRY_CAP_SECONDS", "3600"))
    return min(cap, base * (2 ** max(attempt - 1, 0)))


async def process_claimed_delivery(
    db: AsyncSession, *, delivery_id: UUID, claim_token: UUID
) -> str:
    item = await db.scalar(
        select(IntegrationDelivery)
        .where(
            IntegrationDelivery.id == delivery_id,
            IntegrationDelivery.claim_token == claim_token,
            IntegrationDelivery.status == "IN_FLIGHT",
        )
        .with_for_update()
    )
    if item is None:
        return "STALE_CLAIM"
    await set_tenant_context(db, item.tenant_id)
    connection = await db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == item.connection_id,
            IntegrationConnection.tenant_id == item.tenant_id,
        )
    )
    if connection is None or not connection.enabled:
        result = DeliveryResult(False, False, None, None, "CONNECTION_DISABLED", "Connection disabled.", 0)
    elif not await capability_enabled(db, item.tenant_id, connection.capability_code):
        result = DeliveryResult(
            False, False, None, None, "CAPABILITY_DISABLED", "Live integration capability is disabled.", 0
        )
    else:
        await db.commit()
        try:
            result = await deliver(connection, item)
        except RuntimeError as exc:
            result = DeliveryResult(False, False, None, None, str(exc), "Delivery configuration failed.", 0)
        item = await db.scalar(
            select(IntegrationDelivery)
            .where(
                IntegrationDelivery.id == delivery_id,
                IntegrationDelivery.claim_token == claim_token,
                IntegrationDelivery.status == "IN_FLIGHT",
            )
            .with_for_update()
        )
        if item is None:
            return "STALE_CLAIM"
        await set_tenant_context(db, item.tenant_id)
        connection = await db.scalar(select(IntegrationConnection).where(IntegrationConnection.id == item.connection_id))
        if connection is None:
            return "CONNECTION_MISSING"

    attempt = IntegrationDeliveryAttempt(
        tenant_id=item.tenant_id,
        delivery_id=item.id,
        attempt_number=item.attempts,
        request_hash=item.payload_hash,
        request_timestamp=utcnow(),
        response_status=result.status_code,
        response_hash=result.response_hash,
        outcome="DELIVERED" if result.success else ("RETRY" if result.retryable else "FAILED_TERMINAL"),
        error_code=result.error_code,
        duration_ms=result.duration_ms,
    )
    db.add(attempt)
    item.last_status_code = result.status_code
    item.last_response_hash = result.response_hash
    item.last_error_code = result.error_code
    item.last_error_detail = (result.error_detail or "")[:2000] or None
    item.claim_token = None
    item.claim_expires_at = None
    if result.success:
        item.status = "DELIVERED"
        item.delivered_at = utcnow()
        event_type = "delivery.succeeded"
    elif result.retryable and item.attempts < connection.max_attempts:
        item.status = "RETRY"
        item.next_attempt_at = utcnow() + timedelta(seconds=retry_delay_seconds(item.attempts))
        event_type = "delivery.retry_scheduled"
    else:
        item.status = "FAILED_TERMINAL"
        event_type = "delivery.failed_terminal"
        db.add(
            OperationalException(
                tenant_id=item.tenant_id,
                code="INTEGRATION_DELIVERY_FAILED",
                status="OPEN",
                resource_type="IntegrationDelivery",
                resource_id=item.id,
                detail=f"{connection.name}: {result.error_code or 'DELIVERY_FAILED'}",
            )
        )
    await append_provenance(
        db,
        tenant_id=item.tenant_id,
        chain_scope=f"outbound:{connection.id}",
        payload_hash=item.payload_hash,
        event_type=event_type,
        entity_type="IntegrationDelivery",
        entity_id=item.id,
        metadata={
            "attempt": item.attempts,
            "status_code": result.status_code,
            "response_hash": result.response_hash,
            "error_code": result.error_code,
        },
    )
    await db.commit()
    return item.status


@dataclass(frozen=True)
class ClaimedInbox:
    inbox_id: UUID
    claim_token: UUID


async def claim_inbox_batch(db: AsyncSession, *, limit: int = 50) -> list[ClaimedInbox]:
    now = utcnow()
    lease_seconds = int(os.getenv("INTEGRATION_INBOX_LEASE_SECONDS", "90"))
    items = (
        await db.scalars(
            select(IntegrationInboxMessage)
            .where(
                IntegrationInboxMessage.status.in_(["PENDING", "RETRY", "PROCESSING"]),
                IntegrationInboxMessage.next_attempt_at <= now,
                or_(
                    IntegrationInboxMessage.status != "PROCESSING",
                    IntegrationInboxMessage.claim_expires_at.is_(None),
                    IntegrationInboxMessage.claim_expires_at <= now,
                ),
            )
            .order_by(IntegrationInboxMessage.received_at)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).all()
    claimed: list[ClaimedInbox] = []
    for item in items:
        token = uuid4()
        item.status = "PROCESSING"
        item.claim_token = token
        item.claim_expires_at = now + timedelta(seconds=lease_seconds)
        item.attempts += 1
        claimed.append(ClaimedInbox(item.id, token))
    await db.commit()
    return claimed


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


async def process_claimed_inbox(db: AsyncSession, *, inbox_id: UUID, claim_token: UUID) -> str:
    item = await db.scalar(
        select(IntegrationInboxMessage)
        .where(
            IntegrationInboxMessage.id == inbox_id,
            IntegrationInboxMessage.claim_token == claim_token,
            IntegrationInboxMessage.status == "PROCESSING",
        )
        .with_for_update()
    )
    if item is None:
        return "STALE_CLAIM"
    await set_tenant_context(db, item.tenant_id)
    data = _payload_data(dict(item.payload))
    try:
        if item.event_type in {
            "tracking.position.received",
            "tracking.event.record",
            "tracking.position",
        }:
            load_id = UUID(str(data["load_id"]))
            load = await db.scalar(
                select(Load).where(Load.id == load_id, Load.tenant_id == item.tenant_id)
            )
            if load is None:
                raise ValueError("LOAD_NOT_FOUND")
            occurred_raw = data.get("occurred_at")
            occurred_at = (
                datetime.fromisoformat(str(occurred_raw).replace("Z", "+00:00"))
                if occurred_raw
                else utcnow()
            )
            tracking = TrackingEvent(
                tenant_id=item.tenant_id,
                load_id=load_id,
                event_type=str(data.get("event_type") or item.event_type)[:80],
                occurred_at=occurred_at,
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                payload=data,
            )
            db.add(tracking)
            await db.flush()
            item.translated_type = "TrackingEvent"
            item.translated_resource_id = tracking.id
        elif item.event_type in {"operations.exception.create", "operation.exception.create"}:
            exception = OperationalException(
                tenant_id=item.tenant_id,
                code=str(data.get("code") or "EXTERNAL_OPERATION_EXCEPTION")[:80],
                status="OPEN",
                resource_type=str(data.get("resource_type") or "IntegrationInboxMessage")[:80],
                resource_id=UUID(str(data["resource_id"])) if data.get("resource_id") else item.id,
                assigned_to=None,
                detail=str(data.get("detail") or "External automation requested an operational review.")[:4000],
            )
            db.add(exception)
            await db.flush()
            item.translated_type = "OperationalException"
            item.translated_resource_id = exception.id
        elif item.event_type == "automation.command.requested" or item.event_type.startswith("command."):
            command_type = str(data.get("command_type") or item.event_type.removeprefix("command."))
            allowed_commands = {
                "tracking.event.record",
                "operations.exception.create",
                "document.review.request",
                "shipment.review.request",
            }
            if command_type not in allowed_commands:
                raise ValueError("COMMAND_NOT_ALLOWED")
            command = IntegrationCommandRequest(
                tenant_id=item.tenant_id,
                inbox_id=item.id,
                command_type=command_type,
                command_payload=data,
                status="RECEIVED",
            )
            db.add(command)
            await db.flush()
            item.translated_type = "IntegrationCommandRequest"
            item.translated_resource_id = command.id
        else:
            raise ValueError("UNSUPPORTED_EVENT_TYPE")

        item.status = "PROCESSED"
        item.processed_at = utcnow()
        item.last_error_code = None
        item.last_error_detail = None
        provenance_event = "inbox.processed"
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc).strip("'")[:120] or "INBOX_TRANSLATION_FAILED"
        retryable = code in {"LOAD_NOT_FOUND"} and item.attempts < 5
        item.status = "RETRY" if retryable else "FAILED_TERMINAL"
        item.next_attempt_at = utcnow() + timedelta(seconds=retry_delay_seconds(item.attempts))
        item.last_error_code = code
        item.last_error_detail = "Inbound event could not be translated safely."
        provenance_event = "inbox.retry_scheduled" if retryable else "inbox.failed_terminal"
        if not retryable:
            db.add(
                OperationalException(
                    tenant_id=item.tenant_id,
                    code="INTEGRATION_INBOX_FAILED",
                    status="OPEN",
                    resource_type="IntegrationInboxMessage",
                    resource_id=item.id,
                    detail=f"{item.provider}: {code}",
                )
            )
    item.claim_token = None
    item.claim_expires_at = None
    await append_provenance(
        db,
        tenant_id=item.tenant_id,
        chain_scope=f"inbound:{item.connection_id}",
        payload_hash=item.raw_hash,
        event_type=provenance_event,
        entity_type="IntegrationInboxMessage",
        entity_id=item.id,
        metadata={
            "provider": item.provider,
            "external_event_id": item.external_event_id,
            "translated_type": item.translated_type,
            "error_code": item.last_error_code,
        },
    )
    await db.commit()
    return item.status
