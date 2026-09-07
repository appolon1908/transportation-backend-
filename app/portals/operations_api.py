from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db
from app.integrations.models import IntegrationDelivery, IntegrationInboxMessage
from app.integrations.provenance import append_provenance
from app.models import Load, OperationalException, Shipment, Tender, TrackingEvent
from app.portals.models import PortalClaimSubmission
from app.portals.service import page_rows, serialize_columns
from app.portals.submission_models import PortalCarrierEvidenceSubmission
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1/operations", tags=["operations-portal"])


class ExceptionPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    status: Literal["OPEN", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "DISMISSED"]
    assigned_to: str | None = Field(default=None, max_length=200)
    detail: str | None = Field(default=None, max_length=8_000)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _status_counts(
    db: AsyncSession,
    *,
    model: type,
    tenant_id: UUID,
) -> dict[str, int]:
    rows = (
        await db.execute(
            select(model.status, func.count(model.id))
            .where(model.tenant_id == tenant_id)
            .group_by(model.status)
        )
    ).all()
    return {str(status): int(count) for status, count in rows}


@router.get("/control-tower")
async def control_tower(
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("operations.read")
    since = utcnow() - timedelta(hours=24)
    shipment_counts = await _status_counts(db, model=Shipment, tenant_id=actor.tenant_id)
    load_counts = await _status_counts(db, model=Load, tenant_id=actor.tenant_id)
    tender_counts = await _status_counts(db, model=Tender, tenant_id=actor.tenant_id)
    exception_counts = await _status_counts(
        db,
        model=OperationalException,
        tenant_id=actor.tenant_id,
    )
    delivery_counts = await _status_counts(
        db,
        model=IntegrationDelivery,
        tenant_id=actor.tenant_id,
    )
    inbox_counts = await _status_counts(
        db,
        model=IntegrationInboxMessage,
        tenant_id=actor.tenant_id,
    )
    recent_tracking = int(
        await db.scalar(
            select(func.count(TrackingEvent.id)).where(
                TrackingEvent.tenant_id == actor.tenant_id,
                TrackingEvent.occurred_at >= since,
            )
        )
        or 0
    )
    pending_claims = int(
        await db.scalar(
            select(func.count(PortalClaimSubmission.id)).where(
                PortalClaimSubmission.tenant_id == actor.tenant_id,
                PortalClaimSubmission.status.in_(
                    ["SUBMITTED", "UNDER_REVIEW", "NEEDS_INFORMATION"]
                ),
            )
        )
        or 0
    )
    pending_evidence = int(
        await db.scalar(
            select(func.count(PortalCarrierEvidenceSubmission.id)).where(
                PortalCarrierEvidenceSubmission.tenant_id == actor.tenant_id,
                PortalCarrierEvidenceSubmission.status.in_(["SUBMITTED", "UNDER_REVIEW"]),
            )
        )
        or 0
    )
    failed_deliveries = sum(
        delivery_counts.get(status, 0)
        for status in ("FAILED", "DEAD_LETTER", "TERMINAL")
    )
    open_exceptions = sum(
        exception_counts.get(status, 0)
        for status in ("OPEN", "ACKNOWLEDGED", "IN_PROGRESS")
    )
    at_risk_loads = sum(
        load_counts.get(status, 0)
        for status in ("DELAYED", "EXCEPTION", "AT_RISK")
    )
    return {
        "generated_at": utcnow(),
        "metrics": {
            "open_exceptions": open_exceptions,
            "at_risk_loads": at_risk_loads,
            "failed_integration_deliveries": failed_deliveries,
            "pending_customer_claims": pending_claims,
            "pending_carrier_evidence": pending_evidence,
            "tracking_events_last_24h": recent_tracking,
        },
        "status_counts": {
            "shipments": shipment_counts,
            "loads": load_counts,
            "tenders": tender_counts,
            "exceptions": exception_counts,
            "integration_deliveries": delivery_counts,
            "integration_inbox": inbox_counts,
        },
        "live_effects_enabled": False,
    }


@router.get("/work-queue")
async def operations_work_queue(
    status: str | None = Query(default=None, max_length=40),
    assigned_to: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("operations.read")
    statement = select(OperationalException).where(
        OperationalException.tenant_id == actor.tenant_id
    )
    if status:
        statement = statement.where(OperationalException.status == status.upper())
    else:
        statement = statement.where(
            OperationalException.status.in_(["OPEN", "ACKNOWLEDGED", "IN_PROGRESS"])
        )
    if assigned_to:
        statement = statement.where(OperationalException.assigned_to == assigned_to)
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=OperationalException,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [serialize_columns(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.patch("/exceptions/{exception_id}")
async def update_operational_exception(
    exception_id: UUID,
    payload: ExceptionPatchIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("operations.manage")

    async def action():
        item = await db.scalar(
            select(OperationalException)
            .where(
                OperationalException.id == exception_id,
                OperationalException.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Operational exception not found."},
            )
        if item.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Operational exception version is stale.",
                    "current_version": item.version,
                },
            )
        if item.status in {"RESOLVED", "DISMISSED"} and payload.status not in {
            "RESOLVED",
            "DISMISSED",
        }:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXCEPTION_FINALIZED",
                    "message": "Resolved or dismissed exceptions cannot be reopened here.",
                },
            )
        item.status = payload.status
        item.assigned_to = payload.assigned_to
        item.detail = payload.detail
        item.version += 1
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="operations.exceptions",
            event_type="operations.exception.updated",
            entity_id=str(item.id),
            payload={
                "status": item.status,
                "assigned_to": item.assigned_to,
                "resource_type": item.resource_type,
                "resource_id": str(item.resource_id) if item.resource_id else None,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return serialize_columns(item), "OperationalException", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="operations.exception.update",
        payload={"exception_id": str(exception_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="operations.exception.updated.v1",
        audit_action="OPERATIONS_EXCEPTION_UPDATED",
    )
