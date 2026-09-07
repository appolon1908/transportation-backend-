from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db
from app.integrations.provenance import append_provenance
from app.models import (
    Customer,
    Document,
    Invoice,
    Load,
    LoadShipmentLeg,
    Quote,
    QuoteVersion,
    Shipment,
    ShipmentLeg,
    Stop,
    TrackingEvent,
)
from app.portals.models import PortalClaimSubmission
from app.portals.schemas import ClaimSubmissionIn, QuoteDecisionIn
from app.portals.service import (
    documents_for_resources,
    page_rows,
    require_bound_resource,
    require_portal_binding,
    serialize_columns,
)
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1/portals/customer", tags=["customer-portal"])

CUSTOMER_FIELDS = {
    "id",
    "name",
    "external_reference",
    "currency",
    "is_active",
    "created_at",
    "updated_at",
    "version",
}
QUOTE_FIELDS = {
    "id",
    "customer_id",
    "status",
    "currency",
    "sell_total_minor",
    "buy_total_minor",
    "expires_at",
    "created_at",
    "updated_at",
    "version",
}
QUOTE_VERSION_FIELDS = {
    "id",
    "quote_id",
    "revision",
    "sell_total_minor",
    "buy_total_minor",
    "accessorials",
    "created_at",
    "updated_at",
    "version",
}
SHIPMENT_FIELDS = {
    "id",
    "customer_id",
    "customer_reference",
    "mode",
    "status",
    "created_at",
    "updated_at",
    "version",
}
LEG_FIELDS = {
    "id",
    "shipment_id",
    "sequence",
    "origin_city",
    "origin_region",
    "destination_city",
    "destination_region",
    "pickup_at",
    "delivery_at",
    "created_at",
    "updated_at",
    "version",
}
STOP_FIELDS = {
    "id",
    "shipment_id",
    "sequence",
    "stop_type",
    "city",
    "region",
    "appointment_at",
    "created_at",
    "updated_at",
    "version",
}
LOAD_FIELDS = {
    "id",
    "load_number",
    "equipment_type",
    "status",
    "carrier_id",
    "currency",
    "created_at",
    "updated_at",
    "version",
}
TRACKING_FIELDS = {
    "id",
    "load_id",
    "event_type",
    "occurred_at",
    "latitude",
    "longitude",
    "payload",
    "created_at",
    "updated_at",
    "version",
}
DOCUMENT_FIELDS = {
    "id",
    "load_id",
    "purpose",
    "status",
    "object_key",
    "checksum_sha256",
    "created_at",
    "updated_at",
    "version",
}
INVOICE_FIELDS = {
    "id",
    "customer_id",
    "shipment_id",
    "status",
    "total_minor",
    "currency",
    "created_at",
    "updated_at",
    "version",
}
CLAIM_FIELDS = {
    "id",
    "customer_id",
    "shipment_id",
    "claim_type",
    "title",
    "description",
    "claimed_amount",
    "currency",
    "evidence_document_ids",
    "status",
    "internal_claim_id",
    "customer_visible_note",
    "version",
    "created_at",
    "updated_at",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _binding(db: AsyncSession, actor: Actor, request: Request):
    return await require_portal_binding(
        db,
        actor=actor,
        portal_kind="CUSTOMER",
        capability_code="customer_portal.external_access",
        action=f"{request.method} {request.url.path}",
        correlation_id=request.state.correlation_id,
    )


async def _customer_shipments(
    db: AsyncSession,
    actor: Actor,
    customer_id: UUID,
) -> list[UUID]:
    return list(
        await db.scalars(
            select(Shipment.id).where(
                Shipment.tenant_id == actor.tenant_id,
                Shipment.customer_id == customer_id,
            )
        )
    )


async def _customer_loads_for_shipments(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    shipment_ids: list[UUID],
) -> list[Load]:
    if not shipment_ids:
        return []
    return list(
        (
            await db.scalars(
                select(Load)
                .join(
                    LoadShipmentLeg,
                    (LoadShipmentLeg.load_id == Load.id)
                    & (LoadShipmentLeg.tenant_id == tenant_id),
                )
                .join(
                    ShipmentLeg,
                    (ShipmentLeg.id == LoadShipmentLeg.shipment_leg_id)
                    & (ShipmentLeg.tenant_id == tenant_id),
                )
                .where(
                    Load.tenant_id == tenant_id,
                    ShipmentLeg.shipment_id.in_(shipment_ids),
                )
                .distinct()
            )
        ).all()
    )


@router.get("/context")
async def customer_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    customer = await db.scalar(
        select(Customer).where(
            Customer.tenant_id == actor.tenant_id,
            Customer.id == binding.resource_id,
        )
    )
    if customer is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BOUND_CUSTOMER_MISSING",
                "message": "Bound customer account no longer exists.",
            },
        )
    await db.commit()
    return {
        "portal": "CUSTOMER",
        "binding": {
            "id": binding.id,
            "display_label": binding.display_label,
            "status": binding.status,
            "version": binding.version,
        },
        "customer": serialize_columns(customer, CUSTOMER_FIELDS),
    }


@router.get("/quotes")
async def list_customer_quotes(
    request: Request,
    quote_status: str | None = Query(default=None, alias="status", max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(Quote).where(
        Quote.tenant_id == actor.tenant_id,
        Quote.customer_id == binding.resource_id,
    )
    if quote_status:
        statement = statement.where(Quote.status == quote_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=Quote,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, QUOTE_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/quotes/{quote_id}")
async def customer_quote_detail(
    quote_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    quote = await require_bound_resource(
        db,
        model=Quote,
        actor=actor,
        binding=binding,
        resource_id=quote_id,
        ownership_column="customer_id",
        resource_type="Quote",
        action="customer.quote.read",
        correlation_id=request.state.correlation_id,
    )
    versions = list(
        (
            await db.scalars(
                select(QuoteVersion)
                .where(
                    QuoteVersion.tenant_id == actor.tenant_id,
                    QuoteVersion.quote_id == quote_id,
                )
                .order_by(QuoteVersion.revision.desc())
            )
        ).all()
    )
    await db.commit()
    return {
        "quote": serialize_columns(quote, QUOTE_FIELDS),
        "versions": [serialize_columns(item, QUOTE_VERSION_FIELDS) for item in versions],
    }


@router.post("/quotes/{quote_id}/decision")
async def decide_customer_quote(
    quote_id: UUID,
    payload: QuoteDecisionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)

    async def action():
        quote = await db.scalar(
            select(Quote)
            .where(
                Quote.id == quote_id,
                Quote.tenant_id == actor.tenant_id,
                Quote.customer_id == binding.resource_id,
            )
            .with_for_update()
        )
        if quote is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Quote not found."},
            )
        if quote.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Quote version is stale.",
                    "current_version": quote.version,
                },
            )
        if quote.status not in {"SENT", "OFFERED"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "QUOTE_NOT_DECIDABLE",
                    "message": f"Quote in status {quote.status} cannot be decided.",
                },
            )
        if quote.expires_at and quote.expires_at <= utcnow():
            raise HTTPException(
                status_code=409,
                detail={"code": "QUOTE_EXPIRED", "message": "Quote has expired."},
            )
        quote.status = "ACCEPTED" if payload.decision == "ACCEPT" else "DECLINED"
        quote.version += 1
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.customer.quotes",
            event_type=f"customer.quote.{payload.decision.lower()}",
            entity_id=str(quote.id),
            payload={
                "customer_id": str(binding.resource_id),
                "decision": payload.decision,
                "customer_note": payload.customer_note,
                "quote_version": quote.version,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return serialize_columns(quote, QUOTE_FIELDS), "Quote", quote.id, quote.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="customer.portal.quote.decision",
        payload={"quote_id": str(quote_id), **payload.model_dump(mode="json")},
        action=action,
        event_type=f"customer.portal.quote.{payload.decision.lower()}.v1",
        audit_action="CUSTOMER_PORTAL_QUOTE_DECIDED",
    )


@router.get("/shipments")
async def list_customer_shipments(
    request: Request,
    shipment_status: str | None = Query(default=None, alias="status", max_length=60),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(Shipment).where(
        Shipment.tenant_id == actor.tenant_id,
        Shipment.customer_id == binding.resource_id,
    )
    if shipment_status:
        statement = statement.where(Shipment.status == shipment_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=Shipment,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, SHIPMENT_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/shipments/{shipment_id}")
async def customer_shipment_detail(
    shipment_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    shipment = await require_bound_resource(
        db,
        model=Shipment,
        actor=actor,
        binding=binding,
        resource_id=shipment_id,
        ownership_column="customer_id",
        resource_type="Shipment",
        action="customer.shipment.read",
        correlation_id=request.state.correlation_id,
    )
    legs = list(
        (
            await db.scalars(
                select(ShipmentLeg)
                .where(
                    ShipmentLeg.tenant_id == actor.tenant_id,
                    ShipmentLeg.shipment_id == shipment_id,
                )
                .order_by(ShipmentLeg.sequence)
            )
        ).all()
    )
    stops = list(
        (
            await db.scalars(
                select(Stop)
                .where(
                    Stop.tenant_id == actor.tenant_id,
                    Stop.shipment_id == shipment_id,
                )
                .order_by(Stop.sequence)
            )
        ).all()
    )
    loads = await _customer_loads_for_shipments(
        db,
        tenant_id=actor.tenant_id,
        shipment_ids=[shipment_id],
    )
    load_ids = [item.id for item in loads]
    tracking = list(
        (
            await db.scalars(
                select(TrackingEvent)
                .where(
                    TrackingEvent.tenant_id == actor.tenant_id,
                    TrackingEvent.load_id.in_(load_ids),
                )
                .order_by(TrackingEvent.occurred_at.desc())
                .limit(200)
            )
        ).all()
    ) if load_ids else []
    documents = await documents_for_resources(
        db,
        tenant_id=actor.tenant_id,
        resource_ids=load_ids,
        resource_types=["Load", "load"],
        limit=200,
    )
    await db.commit()
    return {
        "shipment": serialize_columns(shipment, SHIPMENT_FIELDS),
        "legs": [serialize_columns(item, LEG_FIELDS) for item in legs],
        "stops": [serialize_columns(item, STOP_FIELDS) for item in stops],
        "loads": [serialize_columns(item, LOAD_FIELDS) for item in loads],
        "tracking": [serialize_columns(item, TRACKING_FIELDS) for item in tracking],
        "documents": [serialize_columns(item, DOCUMENT_FIELDS) for item in documents],
    }


@router.get("/documents")
async def list_customer_documents(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    shipment_ids = await _customer_shipments(db, actor, binding.resource_id)
    loads = await _customer_loads_for_shipments(
        db,
        tenant_id=actor.tenant_id,
        shipment_ids=shipment_ids,
    )
    items = await documents_for_resources(
        db,
        tenant_id=actor.tenant_id,
        resource_ids=[item.id for item in loads],
        resource_types=["Load", "load"],
        limit=limit,
    )
    await db.commit()
    return {"items": [serialize_columns(item, DOCUMENT_FIELDS) for item in items]}


@router.get("/invoices")
async def list_customer_invoices(
    request: Request,
    invoice_status: str | None = Query(default=None, alias="status", max_length=60),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(Invoice).where(
        Invoice.tenant_id == actor.tenant_id,
        Invoice.customer_id == binding.resource_id,
    )
    if invoice_status:
        statement = statement.where(Invoice.status == invoice_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=Invoice,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, INVOICE_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/claims")
async def list_customer_claims(
    request: Request,
    claim_status: str | None = Query(default=None, alias="status", max_length=60),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(PortalClaimSubmission).where(
        PortalClaimSubmission.tenant_id == actor.tenant_id,
        PortalClaimSubmission.customer_id == binding.resource_id,
    )
    if claim_status:
        statement = statement.where(PortalClaimSubmission.status == claim_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=PortalClaimSubmission,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, CLAIM_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }


@router.post("/claims", status_code=201)
async def submit_customer_claim(
    payload: ClaimSubmissionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    await require_bound_resource(
        db,
        model=Shipment,
        actor=actor,
        binding=binding,
        resource_id=payload.shipment_id,
        ownership_column="customer_id",
        resource_type="Shipment",
        action="customer.claim.submit",
        correlation_id=request.state.correlation_id,
    )
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key is required.",
            },
        )

    loads = await _customer_loads_for_shipments(
        db,
        tenant_id=actor.tenant_id,
        shipment_ids=[payload.shipment_id],
    )
    if payload.evidence_document_ids:
        allowed_documents = await documents_for_resources(
            db,
            tenant_id=actor.tenant_id,
            resource_ids=[item.id for item in loads],
            resource_types=["Load", "load"],
            limit=200,
        )
        allowed_ids = {item.id for item in allowed_documents}
        if not set(payload.evidence_document_ids).issubset(allowed_ids):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_CLAIM_EVIDENCE",
                    "message": "One or more evidence documents are not available to this customer shipment.",
                },
            )

    async def action():
        item = PortalClaimSubmission(
            tenant_id=actor.tenant_id,
            customer_id=binding.resource_id,
            shipment_id=payload.shipment_id,
            submitted_by_subject=actor.subject,
            submission_key=idempotency_key,
            claim_type=payload.claim_type,
            title=payload.title,
            description=payload.description,
            claimed_amount=payload.claimed_amount,
            currency=payload.currency,
            evidence_document_ids=[str(item) for item in payload.evidence_document_ids],
            status="SUBMITTED",
        )
        db.add(item)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.customer.claims",
            event_type="customer.claim.submitted",
            entity_id=str(item.id),
            payload={
                "customer_id": str(item.customer_id),
                "shipment_id": str(item.shipment_id),
                "claim_type": item.claim_type,
                "claimed_amount": str(item.claimed_amount),
                "currency": item.currency,
                "evidence_document_ids": item.evidence_document_ids,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return (
            serialize_columns(item, CLAIM_FIELDS),
            "PortalClaimSubmission",
            item.id,
            item.version,
        )

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="customer.portal.claim.submit",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="customer.portal.claim.submitted.v1",
        audit_action="CUSTOMER_PORTAL_CLAIM_SUBMITTED",
    )
