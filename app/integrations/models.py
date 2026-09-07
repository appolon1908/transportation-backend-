from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationConnection(Base):
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_integration_connection_tenant_name"),
        UniqueConstraint("webhook_slug", name="uq_integration_connection_webhook_slug"),
        Index("ix_integration_connections_tenant_kind", "tenant_id", "kind"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    webhook_slug: Mapped[str] = mapped_column(String(120), nullable=False, default=lambda: uuid4().hex)
    base_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    endpoint_path: Mapped[str | None] = mapped_column(String(500))
    secret_ref: Mapped[str | None] = mapped_column(String(300))
    signing_secret_ref: Mapped[str | None] = mapped_column(String(300))
    signing_key_id: Mapped[str | None] = mapped_column(String(120))
    capability_code: Mapped[str | None] = mapped_column(String(160))
    event_types: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    configuration: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class IntegrationWebhookKey(Base):
    __tablename__ = "integration_webhook_keys"
    __table_args__ = (
        UniqueConstraint("connection_id", "key_id", name="uq_integration_webhook_key"),
        Index("ix_integration_webhook_keys_active", "connection_id", "active"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_id: Mapped[str] = mapped_column(String(120), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationDelivery(Base):
    __tablename__ = "integration_deliveries"
    __table_args__ = (
        UniqueConstraint("connection_id", "outbox_id", name="uq_integration_delivery_outbox"),
        Index("ix_integration_deliveries_claim", "status", "next_attempt_at", "claim_expires_at"),
        Index("ix_integration_deliveries_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_connections.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    outbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("outbox_messages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    last_response_hash: Mapped[str | None] = mapped_column(String(64))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class IntegrationDeliveryAttempt(Base):
    __tablename__ = "integration_delivery_attempts"
    __table_args__ = (
        UniqueConstraint("delivery_id", "attempt_number", name="uq_integration_delivery_attempt"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_deliveries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class IntegrationInboxMessage(Base):
    __tablename__ = "integration_inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "external_event_id", name="uq_integration_inbox_external_event"
        ),
        Index("ix_integration_inbox_claim", "status", "next_attempt_at", "claim_expires_at"),
        Index("ix_integration_inbox_tenant_received", "tenant_id", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_connections.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    event_type: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    key_id: Mapped[str] = mapped_column(String(120), nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    signature_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    headers_redacted: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    translated_type: Mapped[str | None] = mapped_column(String(180))
    translated_resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class IntegrationCommandRequest(Base):
    __tablename__ = "integration_command_requests"
    __table_args__ = (
        UniqueConstraint("inbox_id", name="uq_integration_command_inbox"),
        Index("ix_integration_command_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    inbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_inbox_messages.id", ondelete="RESTRICT"), nullable=False
    )
    command_type: Mapped[str] = mapped_column(String(180), nullable=False)
    command_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RECEIVED", nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationProvenanceEntry(Base):
    __tablename__ = "integration_provenance_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "chain_scope", "sequence", name="uq_integration_provenance_sequence"),
        UniqueConstraint("tenant_id", "entry_hash", name="uq_integration_provenance_hash"),
        Index("ix_integration_provenance_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    chain_scope: Mapped[str] = mapped_column(String(180), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(180), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
