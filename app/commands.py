from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEntry, IdempotencyRecord, OutboxMessage
from app.security import Actor


@dataclass(frozen=True)
class CommandContext:
    actor: Actor
    request_id: str
    correlation_id: str
    idempotency_key: str


async def execute_command(
    *,
    db: AsyncSession,
    request: Request,
    actor: Actor,
    operation: str,
    payload: dict[str, Any],
    action: Callable[[], Awaitable[tuple[dict[str, Any], str, UUID, int]]],
    event_type: str,
    audit_action: str,
) -> dict[str, Any]:
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(status_code=400, detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key header is required."})

    normalized_request = jsonable_encoder(payload)
    request_hash = hashlib.sha256(json.dumps(normalized_request, sort_keys=True).encode()).hexdigest()
    existing = await db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == actor.tenant_id,
            IdempotencyRecord.actor_id == actor.subject,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "Idempotency key was already used for another request."})
        if existing.status == "COMPLETED" and existing.response_json is not None:
            return existing.response_json
        raise HTTPException(status_code=409, detail={"code": "COMMAND_IN_PROGRESS", "message": "Command is already being processed."})

    record = IdempotencyRecord(
        tenant_id=actor.tenant_id,
        actor_id=actor.subject,
        operation=operation,
        key=key,
        request_hash=request_hash,
        status="IN_PROGRESS",
    )
    db.add(record)

    correlation_id = getattr(request.state, "correlation_id", request.headers.get("X-Correlation-Id", ""))
    try:
        response, resource_type, resource_id, aggregate_version = await action()
        normalized_response = jsonable_encoder(response)
        db.add(AuditEntry(
            tenant_id=actor.tenant_id,
            actor_id=actor.subject,
            action=audit_action,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=correlation_id,
            metadata_json={"operation": operation},
        ))
        db.add(OutboxMessage(
            tenant_id=actor.tenant_id,
            event_type=event_type,
            aggregate_type=resource_type,
            aggregate_id=resource_id,
            aggregate_version=aggregate_version,
            payload=normalized_response,
            correlation_id=correlation_id,
            status="PENDING_CONFIGURATION",
        ))
        record.status = "COMPLETED"
        record.response_json = normalized_response
        await db.commit()
        return normalized_response
    except Exception:
        await db.rollback()
        raise
