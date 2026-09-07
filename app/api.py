from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.config import get_settings
from app.db import get_db
from app.models import (
    Capability, Carrier, CarrierCompliance, CarrierContact, CarrierEquipment, CarrierInsurance,
    Customer, CustomerContact, CustomerLocation, Load, LoadShipmentLeg, Quote, QuoteVersion,
    Shipment, ShipmentLeg, Stop, Tender, TrackingEvent,
)
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1")


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    external_reference: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)


class LocationIn(BaseModel):
    name: str
    address1: str
    city: str
    region: str
    postal_code: str
    country: str = "US"


class ContactIn(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None


class CarrierIn(BaseModel):
    legal_name: str
    mc_number: str | None = None
    dot_number: str | None = None


class ShipmentIn(BaseModel):
    customer_id: UUID
    customer_reference: str
    mode: str = "FTL"


class ShipmentLegIn(BaseModel):
    sequence: int = Field(ge=1)
    origin_city: str
    origin_region: str
    destination_city: str
    destination_region: str
    pickup_at: datetime | None = None
    delivery_at: datetime | None = None


class StopIn(BaseModel):
    sequence: int = Field(ge=1)
    stop_type: str
    city: str
    region: str
    appointment_at: datetime | None = None


class LoadIn(BaseModel):
    shipment_leg_id: UUID
    equipment_type: str = "DRY_VAN"


class TenderIn(BaseModel):
    carrier_id: UUID
    rate: Decimal = Field(gt=0)
    currency: str = "USD"
    expires_at: datetime | None = None


class QuoteIn(BaseModel):
    customer_id: UUID
    sell_total_minor: int = Field(ge=0)
    buy_total_minor: int = Field(ge=0)
    currency: str = "USD"
    expires_at: datetime | None = None
    accessorials: dict = Field(default_factory=dict)


class ExpectedVersion(BaseModel):
    expected_version: int = Field(ge=1)


def row(model: object) -> dict:
    return {c.name: getattr(model, c.name) for c in model.__table__.columns}  # type: ignore[attr-defined]


async def tenant_get(db: AsyncSession, model: type, object_id: UUID, actor: Actor):
    obj = await db.scalar(select(model).where(model.id == object_id, model.tenant_id == actor.tenant_id))
    if obj is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Resource not found."})
    return obj


def assert_version(obj: object, expected: int) -> None:
    current = int(getattr(obj, "version"))
    if current != expected:
        raise HTTPException(status_code=412, detail={"code": "STALE_VERSION", "message": "Resource version is stale.", "current_version": current})


def bump(obj: object) -> int:
    version = int(getattr(obj, "version")) + 1
    setattr(obj, "version", version)
    return version


@router.get("/me")
async def me(actor: Actor = Depends(get_actor)):
    return {"subject": actor.subject, "tenant_id": actor.tenant_id, "roles": sorted(actor.roles)}


@router.get("/me/permissions")
async def me_permissions(actor: Actor = Depends(get_actor)):
    return {"permissions": sorted(actor.permissions)}


@router.get("/capabilities")
async def capabilities(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    configured = (await db.scalars(select(Capability).where(Capability.tenant_id == actor.tenant_id))).all()
    overrides = {c.code: c.enabled for c in configured}
    settings = get_settings()
    defaults = {
        "carrier.live_tender_send": settings.capability_live_tender_send,
        "carrier.live_dispatch_notification": settings.capability_live_dispatch_notification,
        "email.live_send": settings.capability_email_live_send,
        "sms.live_send": settings.capability_sms_live_send,
        "accounting.live_export": settings.capability_accounting_live_export,
        "customer_portal.external_access": settings.capability_customer_portal_external_access,
        "carrier_portal.external_access": settings.capability_carrier_portal_external_access,
    }
    return {"capabilities": {**defaults, **overrides}}


@router.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.read")
    items = (await db.scalars(select(Customer).where(Customer.tenant_id == actor.tenant_id).order_by(Customer.name))).all()
    return [row(x) for x in items]


@router.post("/customers", status_code=201)
async def create_customer(payload: CustomerIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.manage")
    async def action():
        item = Customer(tenant_id=actor.tenant_id, name=payload.name.strip(), external_reference=payload.external_reference, currency=payload.currency.upper())
        db.add(item)
        await db.flush()
        return row(item), "Customer", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="customer.create", payload=payload.model_dump(mode="json"), action=action, event_type="customer.created.v1", audit_action="CUSTOMER_CREATED")


@router.get("/customers/{customer_id}")
async def get_customer(customer_id: UUID, response: Response, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.read")
    item = await tenant_get(db, Customer, customer_id, actor)
    response.headers["ETag"] = f'"{item.version}"'
    return row(item)


@router.patch("/customers/{customer_id}")
async def update_customer(customer_id: UUID, payload: CustomerIn, expected_version: int, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.manage")
    async def action():
        item = await tenant_get(db, Customer, customer_id, actor)
        assert_version(item, expected_version)
        item.name = payload.name.strip(); item.external_reference = payload.external_reference; item.currency = payload.currency.upper(); bump(item)
        return row(item), "Customer", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="customer.update", payload={"id": str(customer_id), **payload.model_dump(mode="json"), "expected_version": expected_version}, action=action, event_type="customer.updated.v1", audit_action="CUSTOMER_UPDATED")


@router.get("/customers/{customer_id}/locations")
async def customer_locations(customer_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.read"); await tenant_get(db, Customer, customer_id, actor)
    items = (await db.scalars(select(CustomerLocation).where(CustomerLocation.tenant_id == actor.tenant_id, CustomerLocation.customer_id == customer_id))).all()
    return [row(x) for x in items]


@router.post("/customers/{customer_id}/locations", status_code=201)
async def create_customer_location(customer_id: UUID, payload: LocationIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.manage")
    async def action():
        await tenant_get(db, Customer, customer_id, actor)
        item = CustomerLocation(tenant_id=actor.tenant_id, customer_id=customer_id, **payload.model_dump())
        db.add(item); await db.flush(); return row(item), "CustomerLocation", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="customer.location.create", payload={"customer_id": str(customer_id), **payload.model_dump(mode="json")}, action=action, event_type="customer.location.created.v1", audit_action="CUSTOMER_LOCATION_CREATED")


@router.get("/customers/{customer_id}/contacts")
async def customer_contacts(customer_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.read"); await tenant_get(db, Customer, customer_id, actor)
    items = (await db.scalars(select(CustomerContact).where(CustomerContact.tenant_id == actor.tenant_id, CustomerContact.customer_id == customer_id))).all()
    return [row(x) for x in items]


@router.post("/customers/{customer_id}/contacts", status_code=201)
async def create_customer_contact(customer_id: UUID, payload: ContactIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("customer.manage")
    async def action():
        await tenant_get(db, Customer, customer_id, actor)
        item = CustomerContact(tenant_id=actor.tenant_id, customer_id=customer_id, **payload.model_dump())
        db.add(item); await db.flush(); return row(item), "CustomerContact", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="customer.contact.create", payload={"customer_id": str(customer_id), **payload.model_dump(mode="json")}, action=action, event_type="customer.contact.created.v1", audit_action="CUSTOMER_CONTACT_CREATED")


@router.get("/carriers")
async def list_carriers(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.read")
    items = (await db.scalars(select(Carrier).where(Carrier.tenant_id == actor.tenant_id).order_by(Carrier.legal_name))).all()
    return [row(x) for x in items]


@router.post("/carriers", status_code=201)
async def create_carrier(payload: CarrierIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.manage")
    async def action():
        item = Carrier(tenant_id=actor.tenant_id, legal_name=payload.legal_name.strip(), mc_number=payload.mc_number, dot_number=payload.dot_number)
        db.add(item); await db.flush(); return row(item), "Carrier", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="carrier.create", payload=payload.model_dump(mode="json"), action=action, event_type="carrier.created.v1", audit_action="CARRIER_CREATED")


@router.get("/carriers/{carrier_id}")
async def get_carrier(carrier_id: UUID, response: Response, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.read"); item = await tenant_get(db, Carrier, carrier_id, actor); response.headers["ETag"] = f'"{item.version}"'; return row(item)


@router.patch("/carriers/{carrier_id}")
async def update_carrier(carrier_id: UUID, payload: CarrierIn, expected_version: int, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.manage")
    async def action():
        item = await tenant_get(db, Carrier, carrier_id, actor); assert_version(item, expected_version)
        item.legal_name = payload.legal_name.strip(); item.mc_number = payload.mc_number; item.dot_number = payload.dot_number; bump(item)
        return row(item), "Carrier", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="carrier.update", payload={"id": str(carrier_id), **payload.model_dump(mode="json"), "expected_version": expected_version}, action=action, event_type="carrier.updated.v1", audit_action="CARRIER_UPDATED")


@router.get("/carriers/{carrier_id}/contacts")
async def carrier_contacts(carrier_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.read"); await tenant_get(db, Carrier, carrier_id, actor)
    return [row(x) for x in (await db.scalars(select(CarrierContact).where(CarrierContact.tenant_id == actor.tenant_id, CarrierContact.carrier_id == carrier_id))).all()]


@router.get("/carriers/{carrier_id}/equipment")
async def carrier_equipment(carrier_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.read"); await tenant_get(db, Carrier, carrier_id, actor)
    return [row(x) for x in (await db.scalars(select(CarrierEquipment).where(CarrierEquipment.tenant_id == actor.tenant_id, CarrierEquipment.carrier_id == carrier_id))).all()]


@router.get("/carriers/{carrier_id}/compliance")
async def carrier_compliance(carrier_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.read"); await tenant_get(db, Carrier, carrier_id, actor)
    return [row(x) for x in (await db.scalars(select(CarrierCompliance).where(CarrierCompliance.tenant_id == actor.tenant_id, CarrierCompliance.carrier_id == carrier_id))).all()]


@router.get("/carriers/{carrier_id}/insurance")
async def carrier_insurance(carrier_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.read"); await tenant_get(db, Carrier, carrier_id, actor)
    return [row(x) for x in (await db.scalars(select(CarrierInsurance).where(CarrierInsurance.tenant_id == actor.tenant_id, CarrierInsurance.carrier_id == carrier_id))).all()]


async def carrier_state(carrier_id: UUID, expected_version: int, status_value: str, active: bool, request: Request, db: AsyncSession, actor: Actor):
    actor.require("carrier.compliance.manage")
    async def action():
        item = await tenant_get(db, Carrier, carrier_id, actor); assert_version(item, expected_version)
        item.compliance_status = status_value; item.is_active = active; bump(item)
        return row(item), "Carrier", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation=f"carrier.{status_value.lower()}", payload={"id": str(carrier_id), "expected_version": expected_version}, action=action, event_type="carrier.compliance.changed.v1", audit_action=f"CARRIER_{status_value}")


@router.post("/carriers/{carrier_id}/approve")
async def approve_carrier(carrier_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await carrier_state(carrier_id, payload.expected_version, "APPROVED", True, request, db, actor)


@router.post("/carriers/{carrier_id}/suspend")
async def suspend_carrier(carrier_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await carrier_state(carrier_id, payload.expected_version, "SUSPENDED", False, request, db, actor)


@router.get("/quotes")
async def list_quotes(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("quote.read"); return [row(x) for x in (await db.scalars(select(Quote).where(Quote.tenant_id == actor.tenant_id).order_by(Quote.created_at.desc()))).all()]


@router.post("/quotes", status_code=201)
async def create_quote(payload: QuoteIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("quote.create")
    async def action():
        await tenant_get(db, Customer, payload.customer_id, actor)
        item = Quote(tenant_id=actor.tenant_id, customer_id=payload.customer_id, sell_total_minor=payload.sell_total_minor, buy_total_minor=payload.buy_total_minor, currency=payload.currency.upper(), expires_at=payload.expires_at)
        db.add(item); await db.flush()
        db.add(QuoteVersion(tenant_id=actor.tenant_id, quote_id=item.id, revision=1, sell_total_minor=item.sell_total_minor, buy_total_minor=item.buy_total_minor, accessorials=payload.accessorials))
        return row(item), "Quote", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="quote.create", payload=payload.model_dump(mode="json"), action=action, event_type="quote.created.v1", audit_action="QUOTE_CREATED")


@router.get("/quotes/{quote_id}")
async def get_quote(quote_id: UUID, response: Response, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("quote.read"); item = await tenant_get(db, Quote, quote_id, actor); response.headers["ETag"] = f'"{item.version}"'; versions = (await db.scalars(select(QuoteVersion).where(QuoteVersion.tenant_id == actor.tenant_id, QuoteVersion.quote_id == quote_id).order_by(QuoteVersion.revision))).all(); return {**row(item), "versions": [row(x) for x in versions]}


async def quote_state(quote_id: UUID, expected_version: int, target: str, permission: str, request: Request, db: AsyncSession, actor: Actor):
    actor.require(permission)
    allowed = {"SENT": {"DRAFT", "REVISED"}, "ACCEPTED": {"SENT"}, "DECLINED": {"SENT"}}
    async def action():
        item = await tenant_get(db, Quote, quote_id, actor); assert_version(item, expected_version)
        if item.status not in allowed[target]: raise HTTPException(status_code=409, detail={"code": "INVALID_QUOTE_STATE", "message": f"Cannot transition {item.status} to {target}."})
        item.status = target; bump(item); return row(item), "Quote", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation=f"quote.{target.lower()}", payload={"id": str(quote_id), "expected_version": expected_version}, action=action, event_type=f"quote.{target.lower()}.v1", audit_action=f"QUOTE_{target}")


@router.post("/quotes/{quote_id}/send")
async def send_quote(quote_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await quote_state(quote_id, payload.expected_version, "SENT", "quote.send", request, db, actor)


@router.post("/quotes/{quote_id}/accept")
async def accept_quote(quote_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await quote_state(quote_id, payload.expected_version, "ACCEPTED", "quote.accept", request, db, actor)


@router.post("/quotes/{quote_id}/decline")
async def decline_quote(quote_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await quote_state(quote_id, payload.expected_version, "DECLINED", "quote.accept", request, db, actor)


@router.post("/quotes/{quote_id}/revise")
async def revise_quote(quote_id: UUID, payload: QuoteIn, expected_version: int, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("quote.create")
    async def action():
        item = await tenant_get(db, Quote, quote_id, actor); assert_version(item, expected_version)
        if item.status == "ACCEPTED": raise HTTPException(status_code=409, detail={"code": "QUOTE_ALREADY_ACCEPTED", "message": "Accepted quote cannot be revised."})
        revision = (await db.scalars(select(QuoteVersion).where(QuoteVersion.quote_id == item.id, QuoteVersion.tenant_id == actor.tenant_id))).all()
        item.sell_total_minor = payload.sell_total_minor; item.buy_total_minor = payload.buy_total_minor; item.currency = payload.currency.upper(); item.expires_at = payload.expires_at; item.status = "REVISED"; bump(item)
        db.add(QuoteVersion(tenant_id=actor.tenant_id, quote_id=item.id, revision=len(revision)+1, sell_total_minor=item.sell_total_minor, buy_total_minor=item.buy_total_minor, accessorials=payload.accessorials))
        return row(item), "Quote", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="quote.revise", payload={"id": str(quote_id), **payload.model_dump(mode="json"), "expected_version": expected_version}, action=action, event_type="quote.revised.v1", audit_action="QUOTE_REVISED")


@router.get("/shipments")
async def list_shipments(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.read"); return [row(x) for x in (await db.scalars(select(Shipment).where(Shipment.tenant_id == actor.tenant_id).order_by(Shipment.created_at.desc()))).all()]


@router.post("/shipments", status_code=201)
async def create_shipment(payload: ShipmentIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.manage")
    async def action():
        await tenant_get(db, Customer, payload.customer_id, actor)
        item = Shipment(tenant_id=actor.tenant_id, **payload.model_dump()); db.add(item); await db.flush(); return row(item), "Shipment", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="shipment.create", payload=payload.model_dump(mode="json"), action=action, event_type="shipment.created.v1", audit_action="SHIPMENT_CREATED")


@router.get("/shipments/{shipment_id}")
async def get_shipment(shipment_id: UUID, response: Response, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.read"); item = await tenant_get(db, Shipment, shipment_id, actor); response.headers["ETag"] = f'"{item.version}"'; return row(item)


@router.patch("/shipments/{shipment_id}")
async def update_shipment(shipment_id: UUID, payload: ShipmentIn, expected_version: int, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.manage")
    async def action():
        item = await tenant_get(db, Shipment, shipment_id, actor); assert_version(item, expected_version)
        if item.status not in {"DRAFT", "READY"}: raise HTTPException(status_code=409, detail={"code": "SHIPMENT_LOCKED", "message": "Shipment can no longer be edited."})
        item.customer_id = payload.customer_id; item.customer_reference = payload.customer_reference; item.mode = payload.mode; bump(item); return row(item), "Shipment", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="shipment.update", payload={"id": str(shipment_id), **payload.model_dump(mode="json"), "expected_version": expected_version}, action=action, event_type="shipment.updated.v1", audit_action="SHIPMENT_UPDATED")


@router.post("/shipments/{shipment_id}/cancel")
async def cancel_shipment(shipment_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.manage")
    async def action():
        item = await tenant_get(db, Shipment, shipment_id, actor); assert_version(item, payload.expected_version)
        if item.status in {"DELIVERED", "CANCELLED"}: raise HTTPException(status_code=409, detail={"code": "INVALID_SHIPMENT_STATE", "message": "Shipment cannot be cancelled."})
        item.status = "CANCELLED"; bump(item); return row(item), "Shipment", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="shipment.cancel", payload={"id": str(shipment_id), **payload.model_dump()}, action=action, event_type="shipment.cancelled.v1", audit_action="SHIPMENT_CANCELLED")


@router.get("/shipments/{shipment_id}/legs")
async def list_legs(shipment_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.read"); await tenant_get(db, Shipment, shipment_id, actor); return [row(x) for x in (await db.scalars(select(ShipmentLeg).where(ShipmentLeg.tenant_id == actor.tenant_id, ShipmentLeg.shipment_id == shipment_id).order_by(ShipmentLeg.sequence))).all()]


@router.post("/shipments/{shipment_id}/legs", status_code=201)
async def create_leg(shipment_id: UUID, payload: ShipmentLegIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.manage")
    async def action():
        await tenant_get(db, Shipment, shipment_id, actor); item = ShipmentLeg(tenant_id=actor.tenant_id, shipment_id=shipment_id, **payload.model_dump()); db.add(item); await db.flush(); return row(item), "ShipmentLeg", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="shipment.leg.create", payload={"shipment_id": str(shipment_id), **payload.model_dump(mode="json")}, action=action, event_type="shipment.leg.created.v1", audit_action="SHIPMENT_LEG_CREATED")


@router.get("/shipments/{shipment_id}/stops")
async def list_stops(shipment_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.read"); await tenant_get(db, Shipment, shipment_id, actor); return [row(x) for x in (await db.scalars(select(Stop).where(Stop.tenant_id == actor.tenant_id, Stop.shipment_id == shipment_id).order_by(Stop.sequence))).all()]


@router.post("/shipments/{shipment_id}/stops", status_code=201)
async def create_stop(shipment_id: UUID, payload: StopIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("shipment.manage")
    async def action():
        await tenant_get(db, Shipment, shipment_id, actor); item = Stop(tenant_id=actor.tenant_id, shipment_id=shipment_id, **payload.model_dump()); db.add(item); await db.flush(); return row(item), "Stop", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="shipment.stop.create", payload={"shipment_id": str(shipment_id), **payload.model_dump(mode="json")}, action=action, event_type="shipment.stop.created.v1", audit_action="SHIPMENT_STOP_CREATED")


@router.get("/loads")
async def list_loads(db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("load.read"); return [row(x) for x in (await db.scalars(select(Load).where(Load.tenant_id == actor.tenant_id).order_by(Load.created_at.desc()))).all()]


@router.post("/loads", status_code=201)
async def create_load(payload: LoadIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("load.manage")
    async def action():
        await tenant_get(db, ShipmentLeg, payload.shipment_leg_id, actor)
        item = Load(tenant_id=actor.tenant_id, load_number=f"L-{uuid4().hex[:12].upper()}", equipment_type=payload.equipment_type, status="PLANNED"); db.add(item); await db.flush(); db.add(LoadShipmentLeg(tenant_id=actor.tenant_id, load_id=item.id, shipment_leg_id=payload.shipment_leg_id)); return row(item), "Load", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="load.create", payload=payload.model_dump(mode="json"), action=action, event_type="load.created.v1", audit_action="LOAD_CREATED")


@router.get("/loads/{load_id}")
async def get_load(load_id: UUID, response: Response, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("load.read"); item = await tenant_get(db, Load, load_id, actor); response.headers["ETag"] = f'"{item.version}"'; return row(item)


@router.post("/loads/{load_id}/shipment-legs", status_code=201)
async def attach_leg(load_id: UUID, shipment_leg_id: UUID, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("load.manage")
    async def action():
        load = await tenant_get(db, Load, load_id, actor); await tenant_get(db, ShipmentLeg, shipment_leg_id, actor); item = LoadShipmentLeg(tenant_id=actor.tenant_id, load_id=load_id, shipment_leg_id=shipment_leg_id); db.add(item); bump(load); await db.flush(); return row(item), "Load", load.id, load.version
    return await execute_command(db=db, request=request, actor=actor, operation="load.leg.attach", payload={"load_id": str(load_id), "shipment_leg_id": str(shipment_leg_id)}, action=action, event_type="load.shipment_leg.attached.v1", audit_action="LOAD_LEG_ATTACHED")


@router.delete("/loads/{load_id}/shipment-legs/{leg_id}")
async def detach_leg(load_id: UUID, leg_id: UUID, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("load.manage")
    async def action():
        load = await tenant_get(db, Load, load_id, actor); assignment = await db.scalar(select(LoadShipmentLeg).where(LoadShipmentLeg.tenant_id == actor.tenant_id, LoadShipmentLeg.load_id == load_id, LoadShipmentLeg.shipment_leg_id == leg_id))
        if assignment is None: raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Assignment not found."})
        await db.delete(assignment); bump(load); return {"deleted": True, "load_id": load_id, "shipment_leg_id": leg_id}, "Load", load.id, load.version
    return await execute_command(db=db, request=request, actor=actor, operation="load.leg.detach", payload={"load_id": str(load_id), "shipment_leg_id": str(leg_id)}, action=action, event_type="load.shipment_leg.detached.v1", audit_action="LOAD_LEG_DETACHED")


@router.get("/loads/{load_id}/carrier-search")
async def carrier_search(load_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("carrier.search"); await tenant_get(db, Load, load_id, actor)
    items = (await db.scalars(select(Carrier).where(Carrier.tenant_id == actor.tenant_id, Carrier.is_active.is_(True), Carrier.compliance_status == "APPROVED").order_by(Carrier.legal_name))).all()
    return [row(x) for x in items]


@router.get("/loads/{load_id}/tenders")
async def list_tenders(load_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("tender.read"); await tenant_get(db, Load, load_id, actor); return [row(x) for x in (await db.scalars(select(Tender).where(Tender.tenant_id == actor.tenant_id, Tender.load_id == load_id).order_by(Tender.created_at.desc()))).all()]


@router.post("/loads/{load_id}/tenders", status_code=201)
async def create_tender(load_id: UUID, payload: TenderIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("tender.manage")
    async def action():
        load = await tenant_get(db, Load, load_id, actor); carrier = await tenant_get(db, Carrier, payload.carrier_id, actor)
        if not carrier.is_active or carrier.compliance_status != "APPROVED": raise HTTPException(status_code=409, detail={"code": "CARRIER_NOT_READY", "message": "Carrier must be active and compliance-approved."})
        if load.status not in {"PLANNED", "TENDERED"}: raise HTTPException(status_code=409, detail={"code": "INVALID_LOAD_STATE", "message": "Load cannot be tendered in current state."})
        item = Tender(tenant_id=actor.tenant_id, load_id=load_id, carrier_id=payload.carrier_id, rate=payload.rate, currency=payload.currency.upper(), expires_at=payload.expires_at, status="SENT"); db.add(item); load.status = "TENDERED"; bump(load); await db.flush(); return row(item), "Tender", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="tender.create", payload={"load_id": str(load_id), **payload.model_dump(mode="json")}, action=action, event_type="tender.sent.v1", audit_action="TENDER_SENT")


async def tender_transition(tender_id: UUID, expected_version: int, target: str, request: Request, db: AsyncSession, actor: Actor):
    actor.require("tender.respond")
    async def action():
        tender = await tenant_get(db, Tender, tender_id, actor); assert_version(tender, expected_version)
        if tender.status != "SENT": raise HTTPException(status_code=409, detail={"code": "INVALID_TENDER_STATE", "message": "Only sent tenders may be changed."})
        load = await tenant_get(db, Load, tender.load_id, actor)
        tender.status = target; bump(tender)
        if target == "ACCEPTED":
            if load.status != "TENDERED": raise HTTPException(status_code=409, detail={"code": "LOAD_ALREADY_COVERED", "message": "Load is not available for acceptance."})
            load.carrier_id = tender.carrier_id; load.carrier_rate = tender.rate; load.currency = tender.currency; load.status = "COVERED"; bump(load)
        return row(tender), "Tender", tender.id, tender.version
    return await execute_command(db=db, request=request, actor=actor, operation=f"tender.{target.lower()}", payload={"id": str(tender_id), "expected_version": expected_version}, action=action, event_type=f"tender.{target.lower()}.v1", audit_action=f"TENDER_{target}")


@router.post("/tenders/{tender_id}/accept")
async def accept_tender(tender_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await tender_transition(tender_id, payload.expected_version, "ACCEPTED", request, db, actor)


@router.post("/tenders/{tender_id}/reject")
async def reject_tender(tender_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await tender_transition(tender_id, payload.expected_version, "REJECTED", request, db, actor)


@router.post("/tenders/{tender_id}/withdraw")
async def withdraw_tender(tender_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await tender_transition(tender_id, payload.expected_version, "WITHDRAWN", request, db, actor)


async def load_transition(load_id: UUID, expected_version: int, target: str, allowed: set[str], event: str, permission: str, request: Request, db: AsyncSession, actor: Actor):
    actor.require(permission)
    async def action():
        item = await tenant_get(db, Load, load_id, actor); assert_version(item, expected_version)
        if item.status not in allowed: raise HTTPException(status_code=409, detail={"code": "INVALID_LOAD_STATE", "message": f"Cannot transition {item.status} to {target}."})
        item.status = target; bump(item); return row(item), "Load", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation=f"load.{target.lower()}", payload={"id": str(load_id), "expected_version": expected_version}, action=action, event_type=event, audit_action=f"LOAD_{target}")


@router.post("/loads/{load_id}/dispatch")
async def dispatch_load(load_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await load_transition(load_id, payload.expected_version, "DISPATCHED", {"COVERED"}, "load.dispatched.v1", "load.dispatch", request, db, actor)


@router.post("/loads/{load_id}/arrive")
async def arrive_load(load_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await load_transition(load_id, payload.expected_version, "ARRIVED", {"DISPATCHED", "IN_TRANSIT"}, "load.arrived.v1", "load.dispatch", request, db, actor)


@router.post("/loads/{load_id}/depart")
async def depart_load(load_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await load_transition(load_id, payload.expected_version, "IN_TRANSIT", {"DISPATCHED", "ARRIVED"}, "load.departed.v1", "load.dispatch", request, db, actor)


@router.post("/loads/{load_id}/deliver")
async def deliver_load(load_id: UUID, payload: ExpectedVersion, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    return await load_transition(load_id, payload.expected_version, "DELIVERED", {"IN_TRANSIT", "ARRIVED"}, "load.delivered.v1", "load.dispatch", request, db, actor)


class TrackingIn(BaseModel):
    event_type: str
    occurred_at: datetime
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    payload: dict = Field(default_factory=dict)


@router.get("/loads/{load_id}/tracking")
async def tracking(load_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("tracking.read"); await tenant_get(db, Load, load_id, actor); items = (await db.scalars(select(TrackingEvent).where(TrackingEvent.tenant_id == actor.tenant_id, TrackingEvent.load_id == load_id).order_by(TrackingEvent.occurred_at.desc()))).all(); return [row(x) for x in items]


@router.get("/loads/{load_id}/positions")
async def positions(load_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("tracking.read"); await tenant_get(db, Load, load_id, actor); items = (await db.scalars(select(TrackingEvent).where(TrackingEvent.tenant_id == actor.tenant_id, TrackingEvent.load_id == load_id, TrackingEvent.latitude.is_not(None), TrackingEvent.longitude.is_not(None)).order_by(TrackingEvent.occurred_at.desc()))).all(); return [row(x) for x in items]


@router.post("/loads/{load_id}/tracking/manual-event", status_code=201)
async def manual_tracking(load_id: UUID, payload: TrackingIn, request: Request, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("tracking.manage")
    async def action():
        await tenant_get(db, Load, load_id, actor); item = TrackingEvent(tenant_id=actor.tenant_id, load_id=load_id, **payload.model_dump()); db.add(item); await db.flush(); return row(item), "TrackingEvent", item.id, item.version
    return await execute_command(db=db, request=request, actor=actor, operation="tracking.manual_event", payload={"load_id": str(load_id), **payload.model_dump(mode="json")}, action=action, event_type="tracking.position.received.v1", audit_action="TRACKING_MANUAL_EVENT")
