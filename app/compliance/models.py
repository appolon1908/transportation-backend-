from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompliancePolicy(Base):
    __tablename__ = "carrier_compliance_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_compliance_policy_tenant_version"),
        CheckConstraint("expiry_buffer_days BETWEEN 0 AND 365", name="ck_compliance_expiry_buffer"),
        CheckConstraint("max_safety_age_days BETWEEN 1 AND 730", name="ck_compliance_safety_age"),
        Index("ix_compliance_policy_active", "tenant_id", "enabled", "version"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    authority_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    minimum_auto_liability: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("1000000")
    )
    minimum_cargo: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("100000")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    expiry_buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    max_safety_age_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    allowed_safety_ratings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["SATISFACTORY", "CONDITIONAL", "NOT_RATED"]
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(220), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CarrierAuthorityRecord(Base):
    __tablename__ = "carrier_authority_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "carrier_id", "authority_type", "authority_number_hash",
            name="uq_carrier_authority_identity",
        ),
        Index("ix_carrier_authority_ready", "tenant_id", "carrier_id", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    authority_type: Mapped[str] = mapped_column(String(60), nullable=False)
    authority_number_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    issued_at: Mapped[date | None] = mapped_column(Date)
    expires_at: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(220))
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_by: Mapped[str] = mapped_column(String(220), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CarrierInsuranceRecord(Base):
    __tablename__ = "carrier_insurance_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "carrier_id", "insurance_type", "policy_number_hash",
            name="uq_carrier_insurance_identity",
        ),
        Index(
            "ix_carrier_insurance_ready",
            "tenant_id",
            "carrier_id",
            "insurance_type",
            "status",
            "expires_at",
        ),
        CheckConstraint("limit_amount >= 0", name="ck_carrier_insurance_limit"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    insurance_type: Mapped[str] = mapped_column(String(60), nullable=False)
    policy_number_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    insurer_name: Mapped[str] = mapped_column(String(220), nullable=False)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_at: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[date] = mapped_column(Date, nullable=False)
    evidence_document_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_by: Mapped[str] = mapped_column(String(220), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CarrierSafetyRecord(Base):
    __tablename__ = "carrier_safety_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "carrier_id", "source", "source_record_id",
            name="uq_carrier_safety_source_record",
        ),
        Index("ix_carrier_safety_latest", "tenant_id", "carrier_id", "measured_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[str] = mapped_column(String(60), nullable=False)
    out_of_service: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    measured_at: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(220), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_by: Mapped[str] = mapped_column(String(220), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CarrierComplianceOverride(Base):
    __tablename__ = "carrier_compliance_overrides"
    __table_args__ = (
        Index("ix_compliance_override_active", "tenant_id", "carrier_id", "action", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(220), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(220))


class CarrierReadinessDecision(Base):
    __tablename__ = "carrier_readiness_decisions"
    __table_args__ = (
        Index("ix_readiness_decision_carrier", "tenant_id", "carrier_id", "evaluated_at"),
        Index("ix_readiness_decision_action", "tenant_id", "action", "ready", "evaluated_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_by: Mapped[str] = mapped_column(String(220), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
