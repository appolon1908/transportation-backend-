from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.integrations.models import (
    IntegrationConnection,
    IntegrationDelivery,
    IntegrationInboxMessage,
)
from app.integrations.service import set_tenant_context
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1", tags=["integrations"])
_TERMINAL_FAILURE_STATUSES = {
    "DEAD",
    "DEAD_LETTER",
    "FAILED",
    "FAILED_TERMINAL",
    "PERMANENT_FAILURE",
    "TERMINAL_FAILURE",
}


def count_terminal_failures(*status_counts: Mapping[str, int]) -> int:
    """Count terminal states across independent queues without key collisions."""

    return sum(
        int(count)
        for counts in status_counts
        for status, count in counts.items()
        if str(status).upper() in _TERMINAL_FAILURE_STATUSES
    )


@router.get("/admin/integrations/health")
async def integration_health(
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    """Return a tenant-scoped, credential-free integration health summary."""

    actor.require("integration.retry")
    await set_tenant_context(db, actor.tenant_id, actor.subject)

    connection_total = await db.scalar(
        select(func.count(IntegrationConnection.id)).where(
            IntegrationConnection.tenant_id == actor.tenant_id
        )
    )
    enabled_connection_total = await db.scalar(
        select(func.count(IntegrationConnection.id)).where(
            IntegrationConnection.tenant_id == actor.tenant_id,
            IntegrationConnection.enabled.is_(True),
        )
    )
    delivery_rows = (
        await db.execute(
            select(IntegrationDelivery.status, func.count(IntegrationDelivery.id))
            .where(IntegrationDelivery.tenant_id == actor.tenant_id)
            .group_by(IntegrationDelivery.status)
        )
    ).all()
    inbox_rows = (
        await db.execute(
            select(
                IntegrationInboxMessage.status,
                func.count(IntegrationInboxMessage.id),
            )
            .where(IntegrationInboxMessage.tenant_id == actor.tenant_id)
            .group_by(IntegrationInboxMessage.status)
        )
    ).all()

    deliveries_by_status = {
        str(status).upper(): int(count) for status, count in delivery_rows
    }
    inbox_by_status = {
        str(status).upper(): int(count) for status, count in inbox_rows
    }
    terminal_failures = count_terminal_failures(
        deliveries_by_status,
        inbox_by_status,
    )

    return {
        "status": "degraded" if terminal_failures else "available",
        "tenant_id": str(actor.tenant_id),
        "connections": {
            "total": int(connection_total or 0),
            "enabled": int(enabled_connection_total or 0),
        },
        "deliveries": {
            "by_status": deliveries_by_status,
        },
        "inbox": {
            "by_status": inbox_by_status,
        },
        "terminal_failures": terminal_failures,
    }
