from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class PortalPrincipalBinding(Base):
    __tablename__ = "portal_principal_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "principal_issuer",
            "principal_subject",
            "portal_kind",
            name="uq_portal_principal_kind",
        ),
        CheckConstraint(
            "portal_kind IN ('CUSTOMER', 'CARRIER')",
            name="ck_portal_binding_kind",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
            name="ck_portal_binding_status",
        ),
        Index(
            "ix_portal_binding_lookup",
            "tenant_id",
            "principal_issuer",
            "principal_subject",
            "portal_kind",
            "status",
        ),
        Index(
            "ix_portal_binding_resource",
            "tenant_id",
            "portal_kind",
            "resource_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    principal_issuer: Mapped[str] = mapped_column(String(400), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(220), nullable=False)
    portal_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    display_label: Mapped[str] = mapped_column(String(220), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(220), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(220))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class PortalClaimSubmission(Base):
    __tablename__ = "portal_claim_submissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "customer_id", "submission_key",
            name="uq_portal_claim_submission_key",
        ),
        CheckConstraint(
            "status IN ('SUBMITTED', 'UNDER_REVIEW', 'NEEDS_INFORMATION', 'ACCEPTED', 'DENIED', 'WITHDRAWN')",
            name="ck_portal_claim_status",
        ),
        CheckConstraint("claimed_amount >= 0", name="ck_portal_claim_amount"),
        Index(
            "ix_portal_claim_customer",
            "tenant_id",
            "customer_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_portal_claim_shipment",
            "tenant_id",
            "shipment_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    shipment_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    submitted_by_subject: Mapped[str] = mapped_column(String(220), nullable=False)
    submission_key: Mapped[str] = mapped_column(String(120), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(220), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    evidence_document_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SUBMITTED")
    internal_claim_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    customer_visible_note: Mapped[str | None] = mapped_column(Text)
    internal_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class PortalAccessAudit(Base):
    __tablename__ = "portal_access_audit"
    __table_args__ = (
        Index(
            "ix_portal_access_principal",
            "tenant_id",
            "principal_subject",
            "portal_kind",
            "occurred_at",
        ),
        Index(
            "ix_portal_access_resource",
            "tenant_id",
            "resource_type",
            "resource_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    principal_issuer: Mapped[str] = mapped_column(String(400), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(220), nullable=False)
    portal_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
