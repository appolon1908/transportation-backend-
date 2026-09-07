from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db
from app.integrations.provenance import append_provenance
from app.models import Claim
from app.portals.carrier_schemas import CarrierEvidenceReviewIn
from app.portals.models import PortalClaimSubmission
from app.portals.service import page_rows, serialize_columns
from app.portals.submission_models import PortalCarrierEvidenceSubmission
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1/admin/portal-reviews", tags=["portal-review"])


class ClaimReviewIn(BaseModel):
    expected_version: int = Field(ge=1)
    status: Literal[
        "UNDER_REVIEW",
        "NEEDS_INFORMATION",
        "ACCEPTED",
        "DENIED",
        "WITHDRAWN",
    ]
    customer_visible_note: str | None = Field(default=None, max_length=4_000)
    internal_note: str | None = Field(default=None, max_length=8_000)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/claims")
async def list_claim_reviews(
    review_status: str | None = Query(default=None, alias="status", max_length=30),
    customer_id: UUID | None = Query(default=None),
    shipment_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("claim.read")
    statement = select(PortalClaimSubmission).where(
        PortalClaimSubmission.tenant_id == actor.tenant_id
    )
    if review_status:
        statement = statement.where(
            PortalClaimSubmission.status == review_status.upper()
        )
    if customer_id:
        statement = statement.where(PortalClaimSubmission.customer_id == customer_id)
    if shipment_id:
        statement = statement.where(PortalClaimSubmission.shipment_id == shipment_id)
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=PortalClaimSubmission,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [serialize_columns(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.patch("/claims/{submission_id}")
async def review_claim(
    submission_id: UUID,
    payload: ClaimReviewIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("claim.manage")

    async def action():
        item = await db.scalar(
            select(PortalClaimSubmission)
            .where(
                PortalClaimSubmission.id == submission_id,
                PortalClaimSubmission.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Portal claim submission not found."},
            )
        if item.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Claim submission version is stale.",
                    "current_version": item.version,
                },
            )
        if item.status in {"DENIED", "WITHDRAWN"} and payload.status != item.status:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CLAIM_REVIEW_FINALIZED",
                    "message": "A denied or withdrawn claim cannot be reopened through this endpoint.",
                },
            )

        if payload.status == "ACCEPTED" and item.internal_claim_id is None:
            claim = Claim(
                tenant_id=actor.tenant_id,
                shipment_id=item.shipment_id,
                status="OPEN",
                description=item.description,
            )
            db.add(claim)
            await db.flush()
            item.internal_claim_id = claim.id

        item.status = payload.status
        item.customer_visible_note = payload.customer_visible_note
        item.internal_note = payload.internal_note
        item.version += 1
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.customer.claims",
            event_type="customer.claim.reviewed",
            entity_id=str(item.id),
            payload={
                "status": item.status,
                "internal_claim_id": str(item.internal_claim_id)
                if item.internal_claim_id
                else None,
                "customer_visible_note": item.customer_visible_note,
                "reviewed_at": utcnow().isoformat(),
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return serialize_columns(item), "PortalClaimSubmission", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="portal.claim.review",
        payload={"submission_id": str(submission_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="portal.claim.reviewed.v1",
        audit_action="PORTAL_CLAIM_REVIEWED",
    )


@router.get("/carrier-evidence")
async def list_carrier_evidence_reviews(
    review_status: str | None = Query(default=None, alias="status", max_length=30),
    carrier_id: UUID | None = Query(default=None),
    evidence_type: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("carrier.read")
    statement = select(PortalCarrierEvidenceSubmission).where(
        PortalCarrierEvidenceSubmission.tenant_id == actor.tenant_id
    )
    if review_status:
        statement = statement.where(
            PortalCarrierEvidenceSubmission.status == review_status.upper()
        )
    if carrier_id:
        statement = statement.where(
            PortalCarrierEvidenceSubmission.carrier_id == carrier_id
        )
    if evidence_type:
        statement = statement.where(
            PortalCarrierEvidenceSubmission.evidence_type == evidence_type.upper()
        )
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=PortalCarrierEvidenceSubmission,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [serialize_columns(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.patch("/carrier-evidence/{submission_id}")
async def review_carrier_evidence(
    submission_id: UUID,
    payload: CarrierEvidenceReviewIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("carrier.compliance.manage")
    if payload.status == "ACCEPTED" and payload.authoritative_record_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AUTHORITATIVE_RECORD_REQUIRED",
                "message": "Accepted evidence must reference an authoritative compliance record.",
            },
        )

    async def action():
        item = await db.scalar(
            select(PortalCarrierEvidenceSubmission)
            .where(
                PortalCarrierEvidenceSubmission.id == submission_id,
                PortalCarrierEvidenceSubmission.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NOT_FOUND",
                    "message": "Carrier evidence submission not found.",
                },
            )
        if item.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Evidence submission version is stale.",
                    "current_version": item.version,
                },
            )
        item.status = payload.status
        item.reviewer_note = payload.reviewer_note
        item.authoritative_record_id = payload.authoritative_record_id
        item.reviewed_by = actor.subject
        item.reviewed_at = utcnow()
        item.version += 1
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.carrier.evidence",
            event_type="carrier.evidence.reviewed",
            entity_id=str(item.id),
            payload={
                "carrier_id": str(item.carrier_id),
                "evidence_type": item.evidence_type,
                "status": item.status,
                "authoritative_record_id": str(item.authoritative_record_id)
                if item.authoritative_record_id
                else None,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return (
            serialize_columns(item),
            "PortalCarrierEvidenceSubmission",
            item.id,
            item.version,
        )

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="portal.carrier-evidence.review",
        payload={"submission_id": str(submission_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="portal.carrier-evidence.reviewed.v1",
        audit_action="PORTAL_CARRIER_EVIDENCE_REVIEWED",
    )
