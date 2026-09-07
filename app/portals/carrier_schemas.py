from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TenderResponseIn(BaseModel):
    expected_version: int = Field(ge=1)
    decision: Literal["ACCEPT", "REJECT"]
    note: str | None = Field(default=None, max_length=2_000)


class CarrierTrackingIn(BaseModel):
    source_event_id: str = Field(min_length=4, max_length=220)
    event_type: Literal[
        "EN_ROUTE_TO_PICKUP",
        "ARRIVED_PICKUP",
        "PICKED_UP",
        "IN_TRANSIT",
        "DELAYED",
        "ARRIVED_DELIVERY",
        "DELIVERED",
    ]
    occurred_at: datetime
    latitude: Decimal | None = Field(default=None, ge=Decimal("-90"), le=Decimal("90"))
    longitude: Decimal | None = Field(default=None, ge=Decimal("-180"), le=Decimal("180"))
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coordinates_must_be_paired(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class CarrierEvidenceSubmissionIn(BaseModel):
    evidence_type: Literal[
        "AUTHORITY",
        "AUTO_LIABILITY",
        "CARGO",
        "GENERAL_LIABILITY",
        "WORKERS_COMP",
        "SAFETY",
    ]
    identifier: str | None = Field(default=None, min_length=2, max_length=180, exclude=True)
    evidence_document_ids: list[UUID] = Field(min_length=1, max_length=30)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_identifier_for_numbered_evidence(self):
        if self.evidence_type != "SAFETY" and not self.identifier:
            raise ValueError("identifier is required for authority and insurance evidence")
        return self


class CarrierEvidenceReviewIn(BaseModel):
    expected_version: int = Field(ge=1)
    status: Literal["UNDER_REVIEW", "ACCEPTED", "REJECTED", "SUPERSEDED"]
    reviewer_note: str | None = Field(default=None, max_length=4000)
    authoritative_record_id: UUID | None = None
