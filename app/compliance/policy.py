from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import CarrierReadinessDecision
from app.integrations.provenance import append_provenance


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reasons: list[str]
    policy_version: int | None
    input_hash: str


class CarrierNotReadyError(RuntimeError):
    def __init__(self, result: ReadinessResult):
        super().__init__("Carrier does not satisfy the current compliance policy.")
        self.result = result


async def evaluate_carrier_readiness(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    carrier_id: UUID,
    action: str,
) -> ReadinessResult:
    row = (
        await db.execute(
            text(
                """
                SELECT ready, reasons, policy_version, input_hash
                FROM freight_carrier_readiness(:tenant_id, :carrier_id, :action)
                """
            ),
            {
                "tenant_id": tenant_id,
                "carrier_id": carrier_id,
                "action": action,
            },
        )
    ).mappings().one()
    return ReadinessResult(
        ready=bool(row["ready"]),
        reasons=list(row["reasons"] or []),
        policy_version=row["policy_version"],
        input_hash=str(row["input_hash"]),
    )


async def record_readiness_decision(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    carrier_id: UUID,
    action: str,
    actor_id: str,
    correlation_id: str,
) -> tuple[ReadinessResult, CarrierReadinessDecision]:
    result = await evaluate_carrier_readiness(
        db,
        tenant_id=tenant_id,
        carrier_id=carrier_id,
        action=action,
    )
    decision = CarrierReadinessDecision(
        tenant_id=tenant_id,
        carrier_id=carrier_id,
        action=action,
        ready=result.ready,
        reasons=result.reasons,
        policy_version=result.policy_version,
        input_hash=result.input_hash,
        evaluated_by=actor_id,
        correlation_id=correlation_id,
    )
    db.add(decision)
    await db.flush()
    await append_provenance(
        db,
        tenant_id=tenant_id,
        stream="carrier.compliance",
        event_type="carrier.readiness.evaluated",
        entity_id=str(carrier_id),
        payload={
            "decision_id": str(decision.id),
            "action": action,
            "ready": result.ready,
            "reasons": result.reasons,
            "policy_version": result.policy_version,
            "input_hash": result.input_hash,
        },
        actor_id=actor_id,
        correlation_id=correlation_id,
    )
    return result, decision


async def assert_carrier_ready(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    carrier_id: UUID,
    action: str,
    actor_id: str,
    correlation_id: str,
) -> CarrierReadinessDecision:
    result, decision = await record_readiness_decision(
        db,
        tenant_id=tenant_id,
        carrier_id=carrier_id,
        action=action,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )
    if not result.ready:
        raise CarrierNotReadyError(result)
    return decision
