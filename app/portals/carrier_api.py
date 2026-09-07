from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.compliance.identifiers import hash_identifier
from app.db import get_db
from app.integrations.provenance import append_provenance
from app.models import (
    Carrier,
    CarrierSettlement,
    Document,
    Load,
    LoadShipmentLeg,
    ShipmentLeg,
    Stop,
    Tender,
    TrackingEvent,
)
from app.portals.carrier_schemas import (
    CarrierEvidenceSubmissionIn,
    CarrierTrackingIn,
    TenderResponseIn,
)
from app.portals.submission_models import (
    PortalCarrierEvidenceSubmission,
    PortalTrackingSubmission,
)
from app.portals.service import (
    carrier_load_ids,
    documents_for_resources,
    page_rows,
    require_portal_binding,
    serialize_columns,
)
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1/portals/carrier", tags=["carrier-portal"])

CARRIER_FIELDS = {
    "id",
    "legal_name",
    "mc_number",
    "dot_number",
    "is_active",
    "compliance_status",
    "created_at",
    "updated_at",
    "version",
}
TENDER_FIELDS = {
    "id",
    "load_id",
    "carrier_id",
    "status",
    "rate",
    "currency",
    "expires_at",
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
    "carrier_rate",
    "currency",
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
SETTLEMENT_FIELDS = {
    "id",
    "carrier_id",
    "load_id",
    "status",
    "total_minor",
    "currency",
    "created_at",
    "updated_at",
    "version",
}
EVIDENCE_FIELDS = {
    "id",
    "carrier_id",
    "evidence_type",
    "evidence_document_ids",
    "metadata_json",
    "status",
    "reviewer_note",
    "reviewed_at",
    "authoritative_record_id",
    "created_at",
    "updated_at",
    "version",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _binding(db: AsyncSession, actor: Actor, request: Request):
    return await require_portal_binding(
        db,
        actor=actor,
        portal_kind="CARRIER",
        capability_code="carrier_portal.external_access",
        action=f"{request.method} {request.url.path}",
        correlation_id=request.state.correlation_id,
    )


async def _require_carrier_load(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    carrier_id: UUID,
    load_id: UUID,
    for_update: bool = False,
) -> Load:
    statement = select(Load).where(
        Load.tenant_id == tenant_id,
        Load.id == load_id,
        Load.carrier_id == carrier_id,
    )
    if for_update:
        statement = statement.with_for_update()
    item = await db.scalar(statement)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Assigned load not found."},
        )
    return item


def _tracking_hash(payload: CarrierTrackingIn) -> str:
    normalized = jsonable_encoder(payload.model_dump(mode="json"))
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@router.get("/context")
async def carrier_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    carrier = await db.scalar(
        select(Carrier).where(
            Carrier.tenant_id == actor.tenant_id,
            Carrier.id == binding.resource_id,
        )
    )
    if carrier is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BOUND_CARRIER_MISSING",
                "message": "Bound carrier account no longer exists.",
            },
        )
    await db.commit()
    return {
        "portal": "CARRIER",
        "binding": {
            "id": binding.id,
            "display_label": binding.display_label,
            "status": binding.status,
            "version": binding.version,
        },
        "carrier": serialize_columns(carrier, CARRIER_FIELDS),
    }


@router.get("/tenders")
async def list_carrier_tenders(
    request: Request,
    tender_status: str | None = Query(default=None, alias="status", max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(Tender).where(
        Tender.tenant_id == actor.tenant_id,
        Tender.carrier_id == binding.resource_id,
    )
    if tender_status:
        statement = statement.where(Tender.status == tender_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=Tender,
        limit=limit,
        cursor=cursor,
    )
    load_ids = {item.load_id for item in items}
    loads = {
        item.id: serialize_columns(item, LOAD_FIELDS)
        for item in (
            await db.scalars(
                select(Load).where(
                    Load.tenant_id == actor.tenant_id,
                    Load.id.in_(load_ids),
                )
            )
        ).all()
    } if load_ids else {}
    await db.commit()
    return {
        "items": [
            {
                **serialize_columns(item, TENDER_FIELDS),
                "load": loads.get(item.load_id),
            }
            for item in items
        ],
        "next_cursor": next_cursor,
    }


@router.post("/tenders/{tender_id}/response")
async def respond_to_tender(
    tender_id: UUID,
    payload: TenderResponseIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)

    async def action():
        tender = await db.scalar(
            select(Tender)
            .where(
                Tender.id == tender_id,
                Tender.tenant_id == actor.tenant_id,
                Tender.carrier_id == binding.resource_id,
            )
            .with_for_update()
        )
        if tender is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Tender not found."},
            )
        if tender.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Tender version is stale.",
                    "current_version": tender.version,
                },
            )
        if tender.status not in {"SENT", "PENDING"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TENDER_NOT_RESPONDABLE",
                    "message": f"Tender in status {tender.status} cannot be answered.",
                },
            )
        if tender.expires_at and tender.expires_at <= utcnow():
            tender.status = "EXPIRED"
            tender.version += 1
            raise HTTPException(
                status_code=409,
                detail={"code": "TENDER_EXPIRED", "message": "Tender has expired."},
            )

        load = await db.scalar(
            select(Load)
            .where(
                Load.id == tender.load_id,
                Load.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        )
        if load is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "TENDER_LOAD_MISSING", "message": "Tender load no longer exists."},
            )

        if payload.decision == "ACCEPT":
            if load.carrier_id and load.carrier_id != binding.resource_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "LOAD_ALREADY_ASSIGNED",
                        "message": "Load is already assigned to another carrier.",
                    },
                )
            tender.status = "ACCEPTED"
            load.carrier_id = binding.resource_id
            load.carrier_rate = tender.rate
            if load.status in {"DRAFT", "PLANNED", "TENDERING"}:
                load.status = "TENDER_ACCEPTED"
            load.version += 1
        else:
            tender.status = "REJECTED"
        tender.version += 1

        response = {
            "tender": serialize_columns(tender, TENDER_FIELDS),
            "load": serialize_columns(load, LOAD_FIELDS),
            "decision": payload.decision,
        }
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.carrier.tenders",
            event_type=f"carrier.tender.{payload.decision.lower()}",
            entity_id=str(tender.id),
            payload={
                "tender_id": str(tender.id),
                "load_id": str(load.id),
                "carrier_id": str(binding.resource_id),
                "decision": payload.decision,
                "note": payload.note,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return response, "Tender", tender.id, tender.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.portal.tender.respond",
        payload={"tender_id": str(tender_id), **payload.model_dump(mode="json")},
        action=action,
        event_type=f"carrier.portal.tender.{payload.decision.lower()}.v1",
        audit_action="CARRIER_PORTAL_TENDER_RESPONDED",
    )


@router.get("/loads")
async def list_carrier_loads(
    request: Request,
    load_status: str | None = Query(default=None, alias="status", max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(Load).where(
        Load.tenant_id == actor.tenant_id,
        Load.carrier_id == binding.resource_id,
    )
    if load_status:
        statement = statement.where(Load.status == load_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=Load,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, LOAD_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/loads/{load_id}")
async def carrier_load_detail(
    load_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    load = await _require_carrier_load(
        db,
        tenant_id=actor.tenant_id,
        carrier_id=binding.resource_id,
        load_id=load_id,
    )
    leg_ids = list(
        await db.scalars(
            select(LoadShipmentLeg.shipment_leg_id).where(
                LoadShipmentLeg.tenant_id == actor.tenant_id,
                LoadShipmentLeg.load_id == load_id,
            )
        )
    )
    legs = list(
        (
            await db.scalars(
                select(ShipmentLeg)
                .where(
                    ShipmentLeg.tenant_id == actor.tenant_id,
                    ShipmentLeg.id.in_(leg_ids),
                )
                .order_by(ShipmentLeg.sequence)
            )
        ).all()
    ) if leg_ids else []
    shipment_ids = {item.shipment_id for item in legs}
    stops = list(
        (
            await db.scalars(
                select(Stop)
                .where(
                    Stop.tenant_id == actor.tenant_id,
                    Stop.shipment_id.in_(shipment_ids),
                )
                .order_by(Stop.shipment_id, Stop.sequence)
            )
        ).all()
    ) if shipment_ids else []
    tracking = list(
        (
            await db.scalars(
                select(TrackingEvent)
                .where(
                    TrackingEvent.tenant_id == actor.tenant_id,
                    TrackingEvent.load_id == load_id,
                )
                .order_by(TrackingEvent.occurred_at.desc())
                .limit(200)
            )
        ).all()
    )
    documents = await documents_for_resources(
        db,
        tenant_id=actor.tenant_id,
        resource_ids=[load_id],
        resource_types=["Load", "load"],
        limit=200,
    )
    await db.commit()
    return {
        "load": serialize_columns(load, LOAD_FIELDS),
        "legs": [serialize_columns(item, LEG_FIELDS) for item in legs],
        "stops": [serialize_columns(item, STOP_FIELDS) for item in stops],
        "tracking": [serialize_columns(item, TRACKING_FIELDS) for item in tracking],
        "documents": [serialize_columns(item, DOCUMENT_FIELDS) for item in documents],
    }


@router.post("/loads/{load_id}/tracking", status_code=201)
async def submit_carrier_tracking(
    load_id: UUID,
    payload: CarrierTrackingIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    await _require_carrier_load(
        db,
        tenant_id=actor.tenant_id,
        carrier_id=binding.resource_id,
        load_id=load_id,
    )
    payload_hash = _tracking_hash(payload)

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {
            "scope": (
                f"carrier-tracking:{actor.tenant_id}:{binding.resource_id}:"
                f"{payload.source_event_id}"
            )
        },
    )
    existing = await db.scalar(
        select(PortalTrackingSubmission).where(
            PortalTrackingSubmission.tenant_id == actor.tenant_id,
            PortalTrackingSubmission.carrier_id == binding.resource_id,
            PortalTrackingSubmission.source_event_id == payload.source_event_id,
        )
    )
    if existing is not None:
        await db.commit()
        if existing.payload_hash != payload_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TRACKING_EVENT_ID_COLLISION",
                    "message": "The source event ID was already used with different content.",
                },
            )
        return {
            "duplicate": True,
            "submission": serialize_columns(existing),
        }

    async def action():
        tracking = TrackingEvent(
            tenant_id=actor.tenant_id,
            load_id=load_id,
            event_type=payload.event_type,
            occurred_at=payload.occurred_at,
            latitude=payload.latitude,
            longitude=payload.longitude,
            payload={
                **payload.payload,
                "source": "carrier_portal",
                "source_event_id": payload.source_event_id,
                "carrier_id": str(binding.resource_id),
            },
        )
        db.add(tracking)
        await db.flush()
        submission = PortalTrackingSubmission(
            tenant_id=actor.tenant_id,
            carrier_id=binding.resource_id,
            load_id=load_id,
            source_event_id=payload.source_event_id,
            event_type=payload.event_type,
            occurred_at=payload.occurred_at,
            payload_hash=payload_hash,
            tracking_event_id=tracking.id,
            status="PROCESSED",
            processed_at=utcnow(),
        )
        db.add(submission)
        await db.flush()
        response = {
            "duplicate": False,
            "submission": serialize_columns(submission),
            "tracking_event": serialize_columns(tracking, TRACKING_FIELDS),
        }
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.carrier.tracking",
            event_type="carrier.tracking.submitted",
            entity_id=str(submission.id),
            payload={
                "load_id": str(load_id),
                "carrier_id": str(binding.resource_id),
                "source_event_id": payload.source_event_id,
                "payload_hash": payload_hash,
                "event_type": payload.event_type,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return response, "TrackingEvent", tracking.id, tracking.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.portal.tracking.submit",
        payload={"load_id": str(load_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="carrier.portal.tracking.submitted.v1",
        audit_action="CARRIER_PORTAL_TRACKING_SUBMITTED",
    )


@router.get("/documents")
async def list_carrier_documents(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    load_ids = sorted(
        await carrier_load_ids(
            db,
            tenant_id=actor.tenant_id,
            carrier_id=binding.resource_id,
        ),
        key=str,
    )
    items = await documents_for_resources(
        db,
        tenant_id=actor.tenant_id,
        resource_ids=load_ids,
        resource_types=["Load", "load"],
        limit=limit,
    )
    await db.commit()
    return {"items": [serialize_columns(item, DOCUMENT_FIELDS) for item in items]}


@router.get("/evidence")
async def list_carrier_evidence(
    request: Request,
    evidence_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(PortalCarrierEvidenceSubmission).where(
        PortalCarrierEvidenceSubmission.tenant_id == actor.tenant_id,
        PortalCarrierEvidenceSubmission.carrier_id == binding.resource_id,
    )
    if evidence_status:
        statement = statement.where(
            PortalCarrierEvidenceSubmission.status == evidence_status.upper()
        )
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=PortalCarrierEvidenceSubmission,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, EVIDENCE_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }


@router.post("/evidence", status_code=201)
async def submit_carrier_evidence(
    payload: CarrierEvidenceSubmissionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key is required."},
        )
    metadata_size = len(
        json.dumps(payload.metadata, sort_keys=True, separators=(",", ":"), default=str).encode()
    )
    if metadata_size > 10_000:
        raise HTTPException(
            status_code=422,
            detail={"code": "METADATA_TOO_LARGE", "message": "Evidence metadata exceeds 10 KB."},
        )

    load_ids = await carrier_load_ids(
        db,
        tenant_id=actor.tenant_id,
        carrier_id=binding.resource_id,
    )
    allowed_documents = await documents_for_resources(
        db,
        tenant_id=actor.tenant_id,
        resource_ids=list(load_ids),
        resource_types=["Load", "load"],
        limit=200,
    )
    allowed_ids = {item.id for item in allowed_documents}
    if not set(payload.evidence_document_ids).issubset(allowed_ids):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_CARRIER_EVIDENCE_DOCUMENT",
                "message": "Evidence documents must belong to this carrier's assigned loads.",
            },
        )

    async def action():
        item = PortalCarrierEvidenceSubmission(
            tenant_id=actor.tenant_id,
            carrier_id=binding.resource_id,
            submitted_by_subject=actor.subject,
            submission_key=idempotency_key,
            evidence_type=payload.evidence_type,
            identifier_hash=hash_identifier(payload.identifier) if payload.identifier else None,
            evidence_document_ids=[str(item) for item in payload.evidence_document_ids],
            metadata_json=payload.metadata,
            status="SUBMITTED",
        )
        db.add(item)
        await db.flush()
        response = serialize_columns(item, EVIDENCE_FIELDS)
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="portal.carrier.evidence",
            event_type="carrier.evidence.submitted",
            entity_id=str(item.id),
            payload={
                "carrier_id": str(binding.resource_id),
                "evidence_type": item.evidence_type,
                "identifier_hash": item.identifier_hash,
                "evidence_document_ids": item.evidence_document_ids,
            },
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return response, "PortalCarrierEvidenceSubmission", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.portal.evidence.submit",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="carrier.portal.evidence.submitted.v1",
        audit_action="CARRIER_PORTAL_EVIDENCE_SUBMITTED",
    )


@router.get("/settlements")
async def list_carrier_settlements(
    request: Request,
    settlement_status: str | None = Query(default=None, alias="status", max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    binding = await _binding(db, actor, request)
    statement = select(CarrierSettlement).where(
        CarrierSettlement.tenant_id == actor.tenant_id,
        CarrierSettlement.carrier_id == binding.resource_id,
    )
    if settlement_status:
        statement = statement.where(CarrierSettlement.status == settlement_status.upper())
    items, next_cursor = await page_rows(
        db,
        statement=statement,
        model=CarrierSettlement,
        limit=limit,
        cursor=cursor,
    )
    await db.commit()
    return {
        "items": [serialize_columns(item, SETTLEMENT_FIELDS) for item in items],
        "next_cursor": next_cursor,
    }
