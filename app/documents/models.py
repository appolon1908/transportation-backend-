from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentObject(Base):
    __tablename__ = "document_objects"
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    created_by: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(160), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(1024))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_code: Mapped[str | None] = mapped_column(String(80))
    verification_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
