from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.compliance.identifiers import hash_identifier, validate_evidence_hash
from app.compliance.models import (
    CarrierAuthorityRecord,
    CarrierComplianceOverride,
    CarrierInsuranceRecord,
    CarrierReadinessDecision,
    CarrierSafetyRecord,
    CompliancePolicy,
)
from app.compliance.policy import record_readiness_decision
from app.db import get_db
from app.integrations.provenance import append_provenance
from app.models import Carrier
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1", tags=["carrier-compliance"])


class PolicyIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    enabled: bool = False
    authority_required: bool = True
    minimum_auto_liability: Decimal = Field(default=Decimal("1000000"), ge=0)
    minimum_cargo: Decimal = Field(default=Decimal("100000"), ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    expiry_buffer_days: int = Field(default=7, ge=0, le=365)
    max_safety_age_days: int = Field(default=90, ge=1, le=730)
    allowed_safety_ratings: list[str] = Field(
        default_factory=lambda: ["SATISFACTORY", "CONDITIONAL", "NOT_RATED"],
        min_length=1,
        max_length=10,
    )
    config: dict[str, Any] = Field(default_factory=dict)


class AuthorityIn(BaseModel):
    authority_type: str = Field(min_length=2, max_length=60)
    authority_number: str = Field(min_length=2, max_length=120, exclude=True)
    status: Literal["ACTIVE", "INACTIVE", "REVOKED", "PENDING"]
    issued_at: date | None = None
    expires_at: date | None = None
    source: str = Field(min_length=2, max_length=120)
    source_record_id: str | None = Field(default=None, max_length=220)
    evidence_hash: str
    verified_at: datetime | None = None


class InsuranceIn(BaseModel):
    insurance_type: Literal["AUTO_LIABILITY", "CARGO", "GENERAL_LIABILITY", "WORKERS_COMP"]
    policy_number: str = Field(min_length=2, max_length=160, exclude=True)
    insurer_name: str = Field(min_length=2, max_length=220)
    limit_amount: Decimal = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    status: Literal["ACTIVE", "CANCELLED", "EXPIRED", "PENDING"]
    effective_at: date
    expires_at: date
    evidence_document_id: UUID | None = None
    evidence_hash: str
    verified_at: datetime | None = None


class SafetyIn(BaseModel):
    rating: Literal["SATISFACTORY", "CONDITIONAL", "UNSATISFACTORY", "NOT_RATED"]
    out_of_service: bool = False
    measured_at: date
    source: str = Field(min_length=2, max_length=120)
    source_record_id: str = Field(min_length=1, max_length=220)
    evidence_hash: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    verified_at: datetime | None = None


class OverrideIn(BaseModel):
    action: Literal["TENDER", "DISPATCH", "ASSIGN"]
    reason_code: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=12, max_length=2000)
    starts_at: datetime
    expires_at: datetime


class ReadinessIn(BaseModel):
    action: Literal["TENDER", "DISPATCH", "ASSIGN"]


def _row(model: object) -> dict[str, Any]:
    return {
        column.name: getattr(model, column.name)
        for column in model.__table__.columns  # type: ignore[attr-defined]
    }


async def _carrier(db: AsyncSession, actor: Actor, carrier_id: UUID) -> Carrier:
    item = await db.scalar(
        select(Carrier).where(
            Carrier.id == carrier_id,
            Carrier.tenant_id == actor.tenant_id,
        )
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CARRIER_NOT_FOUND", "message": "Carrier not found."},
        )
    return item


@router.get("/admin/compliance/policies")
async def list_policies(
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("compliance.manage")
    items = (
        await db.scalars(
            select(CompliancePolicy)
            .where(CompliancePolicy.tenant_id == actor.tenant_id)
            .order_by(CompliancePolicy.version.desc())
        )
    ).all()
    return [_row(item) for item in items]


@router.post("/admin/compliance/policies", status_code=201)
async def create_policy(
    payload: PolicyIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("compliance.manage")

    async def action():
        current_version = await db.scalar(
            select(func.coalesce(func.max(CompliancePolicy.version), 0)).where(
                CompliancePolicy.tenant_id == actor.tenant_id
            )
        )
        if payload.enabled:
            current = (
                await db.scalars(
                    select(CompliancePolicy)
                    .where(
                        CompliancePolicy.tenant_id == actor.tenant_id,
                        CompliancePolicy.enabled.is_(True),
                    )
                    .with_for_update()
                )
            ).all()
            for item in current:
                item.enabled = False
        policy = CompliancePolicy(
            tenant_id=actor.tenant_id,
            name=payload.name.strip(),
            version=int(current_version or 0) + 1,
            enabled=payload.enabled,
            authority_required=payload.authority_required,
            minimum_auto_liability=payload.minimum_auto_liability,
            minimum_cargo=payload.minimum_cargo,
            currency=payload.currency.upper(),
            expiry_buffer_days=payload.expiry_buffer_days,
            max_safety_age_days=payload.max_safety_age_days,
            allowed_safety_ratings=[item.upper() for item in payload.allowed_safety_ratings],
            config=payload.config,
            created_by=actor.subject,
        )
        db.add(policy)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="carrier.compliance",
            event_type="compliance.policy.created",
            entity_id=str(policy.id),
            payload=_row(policy),
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return _row(policy), "CompliancePolicy", policy.id, policy.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="compliance.policy.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="compliance.policy.created.v1",
        audit_action="COMPLIANCE_POLICY_CREATED",
    )


@router.get("/carriers/{carrier_id}/compliance")
async def carrier_compliance_record(
    carrier_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("operations.read")
    await _carrier(db, actor, carrier_id)
    authority = (
        await db.scalars(
            select(CarrierAuthorityRecord)
            .where(
                CarrierAuthorityRecord.tenant_id == actor.tenant_id,
                CarrierAuthorityRecord.carrier_id == carrier_id,
            )
            .order_by(CarrierAuthorityRecord.verified_at.desc())
        )
    ).all()
    insurance = (
        await db.scalars(
            select(CarrierInsuranceRecord)
            .where(
                CarrierInsuranceRecord.tenant_id == actor.tenant_id,
                CarrierInsuranceRecord.carrier_id == carrier_id,
            )
            .order_by(CarrierInsuranceRecord.verified_at.desc())
        )
    ).all()
    safety = (
        await db.scalars(
            select(CarrierSafetyRecord)
            .where(
                CarrierSafetyRecord.tenant_id == actor.tenant_id,
                CarrierSafetyRecord.carrier_id == carrier_id,
            )
            .order_by(CarrierSafetyRecord.measured_at.desc())
        )
    ).all()
    overrides = (
        await db.scalars(
            select(CarrierComplianceOverride)
            .where(
                CarrierComplianceOverride.tenant_id == actor.tenant_id,
                CarrierComplianceOverride.carrier_id == carrier_id,
            )
            .order_by(CarrierComplianceOverride.created_at.desc())
        )
    ).all()
    return {
        "carrier_id": carrier_id,
        "authority": [_row(item) for item in authority],
        "insurance": [_row(item) for item in insurance],
        "safety": [_row(item) for item in safety],
        "overrides": [_row(item) for item in overrides],
    }


@router.post("/carriers/{carrier_id}/compliance/authority", status_code=201)
async def add_authority_record(
    carrier_id: UUID,
    payload: AuthorityIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("compliance.manage")
    await _carrier(db, actor, carrier_id)
    try:
        evidence_hash = validate_evidence_hash(payload.evidence_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_EVIDENCE_HASH", "message": str(exc)}) from exc
    if payload.expires_at and payload.issued_at and payload.expires_at <= payload.issued_at:
        raise HTTPException(status_code=422, detail={"code": "INVALID_AUTHORITY_WINDOW", "message": "expires_at must follow issued_at."})

    async def action():
        item = CarrierAuthorityRecord(
            tenant_id=actor.tenant_id,
            carrier_id=carrier_id,
            authority_type=payload.authority_type.upper(),
            authority_number_hash=hash_identifier(payload.authority_number),
            status=payload.status,
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
            source=payload.source,
            source_record_id=payload.source_record_id,
            evidence_hash=evidence_hash,
            verified_at=payload.verified_at or datetime.now(timezone.utc),
            verified_by=actor.subject,
        )
        db.add(item)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="carrier.compliance",
            event_type="carrier.authority.recorded",
            entity_id=str(carrier_id),
            payload={**_row(item), "authority_number_hash": item.authority_number_hash},
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return _row(item), "CarrierAuthorityRecord", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.authority.record",
        payload={**payload.model_dump(mode="json"), "authority_number": "REDACTED"},
        action=action,
        event_type="carrier.authority.recorded.v1",
        audit_action="CARRIER_AUTHORITY_RECORDED",
    )


@router.post("/carriers/{carrier_id}/compliance/insurance", status_code=201)
async def add_insurance_record(
    carrier_id: UUID,
    payload: InsuranceIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("compliance.manage")
    await _carrier(db, actor, carrier_id)
    if payload.expires_at <= payload.effective_at:
        raise HTTPException(status_code=422, detail={"code": "INVALID_INSURANCE_WINDOW", "message": "expires_at must follow effective_at."})
    try:
        evidence_hash = validate_evidence_hash(payload.evidence_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_EVIDENCE_HASH", "message": str(exc)}) from exc

    async def action():
        item = CarrierInsuranceRecord(
            tenant_id=actor.tenant_id,
            carrier_id=carrier_id,
            insurance_type=payload.insurance_type,
            policy_number_hash=hash_identifier(payload.policy_number),
            insurer_name=payload.insurer_name,
            limit_amount=payload.limit_amount,
            currency=payload.currency.upper(),
            status=payload.status,
            effective_at=payload.effective_at,
            expires_at=payload.expires_at,
            evidence_document_id=payload.evidence_document_id,
            evidence_hash=evidence_hash,
            verified_at=payload.verified_at or datetime.now(timezone.utc),
            verified_by=actor.subject,
        )
        db.add(item)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="carrier.compliance",
            event_type="carrier.insurance.recorded",
            entity_id=str(carrier_id),
            payload={**_row(item), "policy_number_hash": item.policy_number_hash},
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return _row(item), "CarrierInsuranceRecord", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.insurance.record",
        payload={**payload.model_dump(mode="json"), "policy_number": "REDACTED"},
        action=action,
        event_type="carrier.insurance.recorded.v1",
        audit_action="CARRIER_INSURANCE_RECORDED",
    )


@router.post("/carriers/{carrier_id}/compliance/safety", status_code=201)
async def add_safety_record(
    carrier_id: UUID,
    payload: SafetyIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("compliance.manage")
    await _carrier(db, actor, carrier_id)
    try:
        evidence_hash = validate_evidence_hash(payload.evidence_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_EVIDENCE_HASH", "message": str(exc)}) from exc

    async def action():
        item = CarrierSafetyRecord(
            tenant_id=actor.tenant_id,
            carrier_id=carrier_id,
            rating=payload.rating,
            out_of_service=payload.out_of_service,
            measured_at=payload.measured_at,
            source=payload.source,
            source_record_id=payload.source_record_id,
            evidence_hash=evidence_hash,
            metrics=payload.metrics,
            verified_at=payload.verified_at or datetime.now(timezone.utc),
            verified_by=actor.subject,
        )
        db.add(item)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="carrier.compliance",
            event_type="carrier.safety.recorded",
            entity_id=str(carrier_id),
            payload=_row(item),
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return _row(item), "CarrierSafetyRecord", item.id, 1

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.safety.record",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="carrier.safety.recorded.v1",
        audit_action="CARRIER_SAFETY_RECORDED",
    )


@router.post("/carriers/{carrier_id}/compliance/overrides", status_code=201)
async def create_override(
    carrier_id: UUID,
    payload: OverrideIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("compliance.override")
    await _carrier(db, actor, carrier_id)
    now = datetime.now(timezone.utc)
    if payload.starts_at >= payload.expires_at or payload.expires_at <= now:
        raise HTTPException(status_code=422, detail={"code": "INVALID_OVERRIDE_WINDOW", "message": "Override must have a future, positive window."})
    if (payload.expires_at - payload.starts_at).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail={"code": "OVERRIDE_TOO_LONG", "message": "Overrides may not exceed 24 hours."})

    async def action():
        item = CarrierComplianceOverride(
            tenant_id=actor.tenant_id,
            carrier_id=carrier_id,
            action=payload.action,
            reason_code=payload.reason_code,
            reason=payload.reason,
            approved_by=actor.subject,
            starts_at=payload.starts_at,
            expires_at=payload.expires_at,
            active=True,
        )
        db.add(item)
        await db.flush()
        await append_provenance(
            db,
            tenant_id=actor.tenant_id,
            stream="carrier.compliance",
            event_type="carrier.compliance.override.created",
            entity_id=str(carrier_id),
            payload=_row(item),
            actor_id=actor.subject,
            correlation_id=request.state.correlation_id,
        )
        return _row(item), "CarrierComplianceOverride", item.id, 1

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="carrier.compliance.override.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="carrier.compliance.override.created.v1",
        audit_action="CARRIER_COMPLIANCE_OVERRIDE_CREATED",
    )


@router.post("/carriers/{carrier_id}/readiness/evaluate")
async def evaluate_readiness(
    carrier_id: UUID,
    payload: ReadinessIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("operations.read")
    await _carrier(db, actor, carrier_id)
    result, decision = await record_readiness_decision(
        db,
        tenant_id=actor.tenant_id,
        carrier_id=carrier_id,
        action=payload.action,
        actor_id=actor.subject,
        correlation_id=request.state.correlation_id,
    )
    await db.commit()
    return {
        "decision_id": decision.id,
        "carrier_id": carrier_id,
        "action": payload.action,
        "ready": result.ready,
        "reasons": result.reasons,
        "policy_version": result.policy_version,
        "input_hash": result.input_hash,
    }


@router.get("/carriers/{carrier_id}/readiness/history")
async def readiness_history(
    carrier_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("operations.read")
    await _carrier(db, actor, carrier_id)
    items = (
        await db.scalars(
            select(CarrierReadinessDecision)
            .where(
                CarrierReadinessDecision.tenant_id == actor.tenant_id,
                CarrierReadinessDecision.carrier_id == carrier_id,
            )
            .order_by(CarrierReadinessDecision.evaluated_at.desc())
            .limit(limit)
        )
    ).all()
    return [_row(item) for item in items]
