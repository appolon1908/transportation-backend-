from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantEntity:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Capability(Base):
    __tablename__ = "capabilities"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_capability_tenant_code"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    code: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Customer(TenantEntity, Base):
    __tablename__ = "customers"
    name: Mapped[str] = mapped_column(String(250))
    external_reference: Mapped[str | None] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CustomerLocation(TenantEntity, Base):
    __tablename__ = "customer_locations"
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    address1: Mapped[str] = mapped_column(String(250))
    city: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    postal_code: Mapped[str] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(2), default="US")


class CustomerContact(TenantEntity, Base):
    __tablename__ = "customer_contacts"
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))


class Carrier(TenantEntity, Base):
    __tablename__ = "carriers"
    legal_name: Mapped[str] = mapped_column(String(250))
    mc_number: Mapped[str | None] = mapped_column(String(40))
    dot_number: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    compliance_status: Mapped[str] = mapped_column(String(40), default="PENDING")


class CarrierContact(TenantEntity, Base):
    __tablename__ = "carrier_contacts"
    carrier_id: Mapped[UUID] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(180))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))


class CarrierEquipment(TenantEntity, Base):
    __tablename__ = "carrier_equipment"
    carrier_id: Mapped[UUID] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"), index=True)
    equipment_type: Mapped[str] = mapped_column(String(60))
    unit_number: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CarrierCompliance(TenantEntity, Base):
    __tablename__ = "carrier_compliance"
    carrier_id: Mapped[UUID] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"), index=True)
    authority_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    safety_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    notes: Mapped[str | None] = mapped_column(Text)


class CarrierInsurance(TenantEntity, Base):
    __tablename__ = "carrier_insurance"
    carrier_id: Mapped[UUID] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"), index=True)
    policy_number: Mapped[str] = mapped_column(String(120))
    coverage_type: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Quote(TenantEntity, Base):
    __tablename__ = "quotes"
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    sell_total_minor: Mapped[int] = mapped_column(Integer, default=0)
    buy_total_minor: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuoteVersion(TenantEntity, Base):
    __tablename__ = "quote_versions"
    __table_args__ = (UniqueConstraint("quote_id", "revision", name="uq_quote_revision"),)
    quote_id: Mapped[UUID] = mapped_column(ForeignKey("quotes.id", ondelete="RESTRICT"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    sell_total_minor: Mapped[int] = mapped_column(Integer)
    buy_total_minor: Mapped[int] = mapped_column(Integer)
    accessorials: Mapped[dict] = mapped_column(JSONB, default=dict)


class Shipment(TenantEntity, Base):
    __tablename__ = "shipments"
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    customer_reference: Mapped[str] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(30), default="FTL")
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")


class ShipmentLeg(TenantEntity, Base):
    __tablename__ = "shipment_legs"
    __table_args__ = (UniqueConstraint("shipment_id", "sequence", name="uq_shipment_leg_sequence"),)
    shipment_id: Mapped[UUID] = mapped_column(ForeignKey("shipments.id", ondelete="RESTRICT"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    origin_city: Mapped[str] = mapped_column(String(120))
    origin_region: Mapped[str] = mapped_column(String(80))
    destination_city: Mapped[str] = mapped_column(String(120))
    destination_region: Mapped[str] = mapped_column(String(80))
    pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Stop(TenantEntity, Base):
    __tablename__ = "stops"
    shipment_id: Mapped[UUID] = mapped_column(ForeignKey("shipments.id", ondelete="RESTRICT"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stop_type: Mapped[str] = mapped_column(String(30))
    city: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    appointment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Load(TenantEntity, Base):
    __tablename__ = "loads"
    __table_args__ = (UniqueConstraint("tenant_id", "load_number", name="uq_load_number"),)
    load_number: Mapped[str] = mapped_column(String(80))
    equipment_type: Mapped[str] = mapped_column(String(60), default="DRY_VAN")
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    carrier_id: Mapped[UUID | None] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"))
    carrier_rate: Mapped[Decimal | None] = mapped_column(Numeric(19, 4))
    currency: Mapped[str] = mapped_column(String(3), default="USD")


class LoadShipmentLeg(TenantEntity, Base):
    __tablename__ = "load_shipment_legs"
    __table_args__ = (UniqueConstraint("tenant_id", "load_id", "shipment_leg_id", name="uq_load_leg"),)
    load_id: Mapped[UUID] = mapped_column(ForeignKey("loads.id", ondelete="RESTRICT"), index=True)
    shipment_leg_id: Mapped[UUID] = mapped_column(ForeignKey("shipment_legs.id", ondelete="RESTRICT"), index=True)


class Tender(TenantEntity, Base):
    __tablename__ = "tenders"
    load_id: Mapped[UUID] = mapped_column(ForeignKey("loads.id", ondelete="RESTRICT"), index=True)
    carrier_id: Mapped[UUID] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="SENT")
    rate: Mapped[Decimal] = mapped_column(Numeric(19, 4))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrackingEvent(TenantEntity, Base):
    __tablename__ = "tracking_events"
    load_id: Mapped[UUID] = mapped_column(ForeignKey("loads.id", ondelete="RESTRICT"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class Document(TenantEntity, Base):
    __tablename__ = "documents"
    load_id: Mapped[UUID | None] = mapped_column(ForeignKey("loads.id", ondelete="RESTRICT"), index=True)
    purpose: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(40), default="PENDING_UPLOAD")
    object_key: Mapped[str | None] = mapped_column(String(500))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))


class Invoice(TenantEntity, Base):
    __tablename__ = "invoices"
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    shipment_id: Mapped[UUID | None] = mapped_column(ForeignKey("shipments.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    total_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")


class CarrierSettlement(TenantEntity, Base):
    __tablename__ = "carrier_settlements"
    carrier_id: Mapped[UUID] = mapped_column(ForeignKey("carriers.id", ondelete="RESTRICT"), index=True)
    load_id: Mapped[UUID | None] = mapped_column(ForeignKey("loads.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    total_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")


class Claim(TenantEntity, Base):
    __tablename__ = "claims"
    shipment_id: Mapped[UUID] = mapped_column(ForeignKey("shipments.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="OPEN")
    description: Mapped[str] = mapped_column(Text)


class OperationalException(TenantEntity, Base):
    __tablename__ = "operational_exceptions"
    code: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), default="OPEN")
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    assigned_to: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    actor_id: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    correlation_id: Mapped[str] = mapped_column(String(80))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "actor_id", "operation", "key", name="uq_idempotency_scope"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    actor_id: Mapped[str] = mapped_column(String(200))
    operation: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="IN_PROGRESS")
    response_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(String(160), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    aggregate_version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSONB)
    destination: Mapped[str] = mapped_column(String(120), default="freight-events")
    status: Mapped[str] = mapped_column(String(40), default="PENDING_CONFIGURATION")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (UniqueConstraint("provider", "external_event_id", name="uq_inbox_external_event"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    provider: Mapped[str] = mapped_column(String(120))
    external_event_id: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(160))
    raw_hash: Mapped[str] = mapped_column(String(64))
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
