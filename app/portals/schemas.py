from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class BindingIn(BaseModel):
    principal_issuer: str = Field(min_length=8, max_length=400)
    principal_subject: str = Field(min_length=1, max_length=220)
    portal_kind: Literal["CUSTOMER", "CARRIER"]
    resource_id: UUID
    display_label: str = Field(min_length=1, max_length=220)
    status: Literal["ACTIVE", "SUSPENDED"] = "ACTIVE"
    metadata: dict[str, Any] = Field(default_factory=dict)


class BindingPatch(BaseModel):
    expected_version: int = Field(ge=1)
    display_label: str = Field(min_length=1, max_length=220)
    status: Literal["ACTIVE", "SUSPENDED", "REVOKED"]
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuoteDecisionIn(BaseModel):
    expected_version: int = Field(ge=1)
    decision: Literal["ACCEPT", "DECLINE"]
    customer_note: str | None = Field(default=None, max_length=2_000)


class ClaimSubmissionIn(BaseModel):
    shipment_id: UUID
    claim_type: Literal[
        "CARGO_DAMAGE",
        "CARGO_LOSS",
        "SERVICE_FAILURE",
        "OVERCHARGE",
        "OTHER",
    ]
    title: str = Field(min_length=3, max_length=220)
    description: str = Field(min_length=20, max_length=8000)
    claimed_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    evidence_document_ids: list[UUID] = Field(default_factory=list, max_length=40)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class ClaimTriageIn(BaseModel):
    expected_version: int = Field(ge=1)
    status: Literal[
        "UNDER_REVIEW",
        "NEEDS_INFORMATION",
        "ACCEPTED",
        "DENIED",
        "WITHDRAWN",
    ]
    customer_visible_note: str | None = Field(default=None, max_length=4000)
    internal_note: str | None = Field(default=None, max_length=8000)
    internal_claim_id: UUID | None = None


class TenderResponseIn(BaseModel):
    expected_version: int = Field(ge=1)
    decision: Literal["ACCEPT", "REJECT"]
    note: str | None = Field(default=None, max_length=2000)


class TrackingSubmissionIn(BaseModel):
    event_type: Literal[
        "DRIVER_EN_ROUTE_TO_PICKUP",
        "ARRIVED_PICKUP",
        "DEPARTED_PICKUP",
        "IN_TRANSIT",
        "ARRIVED_DELIVERY",
        "DELIVERED",
        "DELAYED",
        "EXCEPTION",
    ]
    occurred_at: datetime
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=2000)
    source_event_id: str = Field(min_length=3, max_length=220)


class DispatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    carrier_id: UUID
    dispatch_note: str | None = Field(default=None, max_length=2000)


class PortalQuery(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
