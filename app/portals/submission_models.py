from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortalTrackingSubmission(Base):
    __tablename__ = "portal_tracking_submissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "carrier_id", "source_event_id",
            name="uq_portal_tracking_source_event",
        ),
        CheckConstraint(
            "status IN ('ACCEPTED', 'PROCESSED', 'REJECTED')",
            name="ck_portal_tracking_status",
        ),
        Index(
            "ix_portal_tracking_load",
            "tenant_id",
            "load_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    load_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(220), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACCEPTED")
    error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalCarrierEvidenceSubmission(Base):
    __tablename__ = "portal_carrier_evidence_submissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "carrier_id", "submission_key",
            name="uq_portal_carrier_evidence_submission_key",
        ),
        CheckConstraint(
            "evidence_type IN ('AUTHORITY', 'AUTO_LIABILITY', 'CARGO', 'GENERAL_LIABILITY', 'WORKERS_COMP', 'SAFETY')",
            name="ck_portal_evidence_type",
        ),
        CheckConstraint(
            "status IN ('SUBMITTED', 'UNDER_REVIEW', 'ACCEPTED', 'REJECTED', 'SUPERSEDED')",
            name="ck_portal_evidence_status",
        ),
        Index(
            "ix_portal_evidence_carrier",
            "tenant_id",
            "carrier_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    carrier_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    submitted_by_subject: Mapped[str] = mapped_column(String(220), nullable=False)
    submission_key: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)
    identifier_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_document_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SUBMITTED")
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(220))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authoritative_record_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
