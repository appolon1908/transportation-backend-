from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadIn(InputModel):
    load_id: UUID
    filename: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_. -]*$")
    content_type: Literal["application/pdf", "image/png", "image/jpeg"]
    size_bytes: int = Field(ge=1, le=10_485_760)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["POD", "BOL", "INVOICE", "CARRIER_EVIDENCE", "OTHER"]


class VersionIn(InputModel):
    expected_version: int = Field(ge=1)


class AttachIn(VersionIn):
    document_id: UUID


class DocumentOut(BaseModel):
    id: UUID
    load_id: UUID | None
    purpose: str
    status: str
    version: int
    checksum_sha256: str | None
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    upload_expires_at: datetime | None = None
    retain_until: datetime | None = None
    rejection_code: str | None = None
