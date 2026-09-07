from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import ExpectedVersion, assert_version, bump, row, tenant_get
from app.commands import execute_command
from app.config import get_settings
from app.db import get_db
from app.models import Capability, CarrierSettlement, Claim, Document, InboxMessage, Invoice, OperationalException, OutboxMessage
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1")


class InvoiceIn(BaseModel):
    customer_id: UUID
    shipment_id: UUID | None = None
    total_minor: int = Field(ge=0)
    currency: str = "USD"


class SettlementIn(BaseModel):
    carrier_id: UUID
    load_id: UUID | None = None
    total_minor: int = Field(ge=0)
    currency: str = "USD"


class ClaimIn(BaseModel):
    shipment_id: UUID
    description: str = Field(min_length=1)


class ExceptionAction(BaseModel):
    expected_version: int = Field(ge=1)
    assigned_to: str | None = None


class CapabilityPatch(BaseModel):
    enabled: bool


@router.get("/loads/{load_id}/exceptions")
async def load_exceptions(load_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("tracking.read")
    items = (await db.scalars(select(OperationalException).where(OperationalException.tenant_id == actor.tenant_id, OperationalException.resource_type == "Load", OperationalException.resource_id == load_id).order_by(OperationalException.created_at.desc()))).all()
    return [row(x) for x in items]


@router.post("/integrations/tracking/{provider}/webhooks", status_code=202)
async def tracking_webhook(provider: str, request: Request, x_webhook_id: str = Header(alias="X-Webhook-Id"), x_webhook_timestamp: str = Header(alias="X-Webhook-Timestamp"), x_webhook_signature: str = Header(alias="X-Webhook-Signature"), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    secret = settings.tracking_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail={"code": "WEBHOOK_NOT_CONFIGURED", "message": "Tracking webhook secret is not configured."})
    try:
        timestamp = int(x_webhook_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail={"code": "INVALID_WEBHOOK_TIMESTAMP", "message": "Invalid timestamp."}) from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(status_code=401, detail={"code": "WEBHOOK_TIMESTAMP_EXPIRED", "message": "Webhook timestamp is outside tolerance."})
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail={"code": "WEBHOOK_TOO_LARGE", "message": "Webhook body exceeds size limit."})
    expected = hmac.new(secret.encode(), f"{x_webhook_timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_webhook_signature):
        raise HTTPException(status_code=401, detail={"code": "INVALID_WEBHOOK_SIGNATURE", "message": "Webhook signature validation failed."})
    try:
        payload = json.loads(raw)
        tenant_id = UUID(str(payload["tenant_id"]))
        event_type = str(payload["type"])
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_WEBHOOK_PAYLOAD", "message": "tenant_id and type are required."}) from exc
    existing = await db.scalar(select(InboxMessage).where(InboxMessage.provider == provider, InboxMessage.external_event_id == x_webhook_id))
    if existing:
        return {"accepted": True, "duplicate": True, "inbox_id": existing.id}
    inbox = InboxMessage(tenant_id=tenant_id, provider=provider, external_event_id=x_webhook_id, event_type=event_type, raw_hash=hashlib.sha256(raw).hexdigest(), signature_verified=True, payload=payload, status="PENDING")
    db.add(inbox)
    await db.commit()
    return {"accepted": True, "duplicate": False, "inbox_id": inbox.id}


@router.post("/documents/upload-sessions")
async def create_upload_session(actor: Actor = Depends(get_actor)):
    actor.require("document.manage")
    raise HTTPException(status_code=503, detail={"code": "STORAGE_NOT_CONFIGURED", "message": "Secure object storage adapter is not configured. No upload session was created."})


@router.post("/documents/{document_id}/confirm")
async def confirm_document(document_id: UUID, actor: Actor = Depends(get_actor)):
    actor.require("document.manage")
    raise HTTPException(status_code=503, detail={"code": "STORAGE_NOT_CONFIGURED", "message": "Document confirmation is disabled until object storage and malware scanning are configured."})


@router.get("/loads/{load_id}/documents")
async def load_documents(load_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("document.read")
    items = (await db.scalars(select(Document).where(Document.tenant_id == actor.tenant_id, Document.load_id == load_id))).all()
    return [row(x) for x in items]


@router.post("/loads/{load_id}/documents")
async def attach_document(load_id: UUID, actor: Actor = Depends(get_actor)):
    actor.require("document.manage")
    raise HTTPException(status_code=503, detail={"code": "STORAGE_NOT_CONFIGURED", "message": "Document attachment is disabled until secure storage is configured."})


@router.post("/loads/{load_id}/pod")
async def upload_pod(load_id: UUID, actor: Actor = Depends(get_actor)):
    actor.require("document.manage")
    raise HTTPException(status_code=503, detail={"code": "STORAGE_NOT_CONFIGURED", "message": "POD upload is disabled until secure storage is configured."})


@router.get("/invoices")
async def list_invoices(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("invoice.read")
    return [row(x) for x in (await db.scalars(select(Invoice).where(Invoice.tenant_id == actor.tenant_id).order_by(Invoice.created_at.desc()))).all()]


@router.post("/invoices", status_code=201)
async def create_invoice(payload: InvoiceIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("invoice.manage")
    async def action():
        item = Invoice(tenant_id=actor.tenant_id, customer_id=payload.customer_id, shipment_id=payload.shipment_id, total_minor=payload.total_minor, currency=payload.currency.upper())
        db.add(item); await db.flush(); return row(item), "Invoice", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="invoice.create", payload=payload.model_dump(mode="json"), action=action, event_type="invoice.created.v1", audit_action="INVOICE_CREATED")


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("invoice.read"); return row(await tenant_get(db, Invoice, invoice_id, actor))


async def invoice_transition(invoice_id: UUID, expected_version: int, target: str, request: Request, db: AsyncSession, actor: Actor):
    actor.require("invoice.manage")
    async def action():
        item = await tenant_get(db, Invoice, invoice_id, actor); assert_version(item, expected_version)
        allowed = {"APPROVED": {"DRAFT"}, "VOID": {"DRAFT", "APPROVED"}}
        if item.status not in allowed[target]: raise HTTPException(status_code=409, detail={"code": "INVALID_INVOICE_STATE", "message": "Invoice transition is not allowed."})
        item.status = target; bump(item); return row(item), "Invoice", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation=f"invoice.{target.lower()}", payload={"id": str(invoice_id), "expected_version": expected_version}, action=action, event_type=f"invoice.{target.lower()}.v1", audit_action=f"INVOICE_{target}")


@router.post("/invoices/{invoice_id}/approve")
async def approve_invoice(invoice_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await invoice_transition(invoice_id, payload.expected_version, "APPROVED", request, db, actor)


@router.post("/invoices/{invoice_id}/void")
async def void_invoice(invoice_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await invoice_transition(invoice_id, payload.expected_version, "VOID", request, db, actor)


@router.get("/carrier-settlements")
async def list_settlements(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("settlement.read"); return [row(x) for x in (await db.scalars(select(CarrierSettlement).where(CarrierSettlement.tenant_id == actor.tenant_id).order_by(CarrierSettlement.created_at.desc()))).all()]


@router.post("/carrier-settlements", status_code=201)
async def create_settlement(payload: SettlementIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("settlement.manage")
    async def action():
        item = CarrierSettlement(tenant_id=actor.tenant_id, carrier_id=payload.carrier_id, load_id=payload.load_id, total_minor=payload.total_minor, currency=payload.currency.upper()); db.add(item); await db.flush(); return row(item), "CarrierSettlement", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="settlement.create", payload=payload.model_dump(mode="json"), action=action, event_type="settlement.created.v1", audit_action="SETTLEMENT_CREATED")


@router.get("/carrier-settlements/{settlement_id}")
async def get_settlement(settlement_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("settlement.read"); return row(await tenant_get(db, CarrierSettlement, settlement_id, actor))


@router.post("/carrier-settlements/{settlement_id}/approve")
async def approve_settlement(settlement_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("settlement.manage")
    async def action():
        item = await tenant_get(db, CarrierSettlement, settlement_id, actor); assert_version(item, payload.expected_version)
        if item.status != "DRAFT": raise HTTPException(status_code=409, detail={"code": "INVALID_SETTLEMENT_STATE", "message": "Only draft settlements may be approved."})
        item.status = "APPROVED"; bump(item); return row(item), "CarrierSettlement", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="settlement.approve", payload={"id": str(settlement_id), **payload.model_dump()}, action=action, event_type="settlement.approved.v1", audit_action="SETTLEMENT_APPROVED")


@router.get("/claims")
async def list_claims(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("claim.read"); return [row(x) for x in (await db.scalars(select(Claim).where(Claim.tenant_id == actor.tenant_id).order_by(Claim.created_at.desc()))).all()]


@router.post("/claims", status_code=201)
async def create_claim(payload: ClaimIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("claim.manage")
    async def action():
        item = Claim(tenant_id=actor.tenant_id, shipment_id=payload.shipment_id, description=payload.description); db.add(item); await db.flush(); return row(item), "Claim", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="claim.create", payload=payload.model_dump(mode="json"), action=action, event_type="claim.created.v1", audit_action="CLAIM_CREATED")


@router.get("/claims/{claim_id}")
async def get_claim(claim_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("claim.read"); return row(await tenant_get(db, Claim, claim_id, actor))


@router.get("/operations/exceptions")
async def operations_exceptions(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("operations.read"); return [row(x) for x in (await db.scalars(select(OperationalException).where(OperationalException.tenant_id == actor.tenant_id).order_by(OperationalException.created_at.desc()))).all()]


async def exception_transition(exception_id: UUID, payload: ExceptionAction, target: str, request: Request, db: AsyncSession, actor: Actor):
    actor.require("operations.manage")
    async def action():
        item = await tenant_get(db, OperationalException, exception_id, actor); assert_version(item, payload.expected_version); item.status = target
        if payload.assigned_to is not None: item.assigned_to = payload.assigned_to
        bump(item); return row(item), "OperationalException", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation=f"exception.{target.lower()}", payload={"id": str(exception_id), **payload.model_dump()}, action=action, event_type=f"operations.exception.{target.lower()}.v1", audit_action=f"EXCEPTION_{target}")


@router.post("/operations/exceptions/{exception_id}/acknowledge")
async def acknowledge_exception(exception_id: UUID, payload: ExceptionAction, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await exception_transition(exception_id, payload, "ACKNOWLEDGED", request, db, actor)


@router.post("/operations/exceptions/{exception_id}/assign")
async def assign_exception(exception_id: UUID, payload: ExceptionAction, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await exception_transition(exception_id, payload, "ASSIGNED", request, db, actor)


@router.post("/operations/exceptions/{exception_id}/resolve")
async def resolve_exception(exception_id: UUID, payload: ExceptionAction, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await exception_transition(exception_id, payload, "RESOLVED", request, db, actor)


@router.get("/operations/dead-letters")
async def dead_letters(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("integration.retry"); items = (await db.scalars(select(OutboxMessage).where(OutboxMessage.tenant_id == actor.tenant_id, OutboxMessage.status == "FAILED_TERMINAL").order_by(OutboxMessage.created_at.desc()))).all(); return [row(x) for x in items]


@router.post("/operations/dead-letters/{message_id}/replay")
async def replay_dead_letter(message_id: UUID, actor: Actor = Depends(get_actor), db: AsyncSession = Depends(get_db)):
    actor.require("integration.retry"); item = await db.scalar(select(OutboxMessage).where(OutboxMessage.id == message_id, OutboxMessage.tenant_id == actor.tenant_id))
    if item is None: raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Dead letter not found."})
    item.status = "PENDING_CONFIGURATION"; item.attempts = 0; await db.commit(); return {"replayed": True, "id": item.id}


@router.get("/admin/users")
async def admin_users(actor: Actor = Depends(get_actor)):
    actor.require("admin.users.manage")
    raise HTTPException(status_code=501, detail={"code": "IDENTITY_DIRECTORY_NOT_IMPLEMENTED", "message": "Identity directory persistence is scheduled for the authentication PR."})


@router.get("/admin/roles")
async def admin_roles(actor: Actor = Depends(get_actor)):
    actor.require("admin.users.manage"); return {"roles": ["admin", "operations", "dispatcher", "finance", "customer", "carrier"]}


@router.get("/admin/permissions")
async def admin_permissions(actor: Actor = Depends(get_actor)):
    actor.require("admin.users.manage"); return {"permissions": ["customer.read", "customer.manage", "carrier.read", "carrier.manage", "carrier.search", "carrier.compliance.manage", "quote.read", "quote.create", "quote.send", "quote.accept", "shipment.read", "shipment.manage", "load.read", "load.manage", "load.dispatch", "tender.read", "tender.manage", "tender.respond", "tracking.read", "tracking.manage", "document.read", "document.manage", "invoice.read", "invoice.manage", "settlement.read", "settlement.manage", "claim.read", "claim.manage", "operations.read", "operations.manage", "integration.retry", "admin.users.manage"]}


@router.get("/admin/capabilities")
async def admin_capabilities(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("admin.users.manage"); return [row(x) for x in (await db.scalars(select(Capability).where(Capability.tenant_id == actor.tenant_id).order_by(Capability.code))).all()]


@router.patch("/admin/capabilities/{code}")
async def patch_capability(code: str, payload: CapabilityPatch, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("admin.users.manage")
    item = await db.scalar(select(Capability).where(Capability.tenant_id == actor.tenant_id, Capability.code == code))
    if item is None:
        item = Capability(tenant_id=actor.tenant_id, code=code, enabled=False); db.add(item)
    item.enabled = payload.enabled
    await db.commit(); await db.refresh(item); return row(item)
