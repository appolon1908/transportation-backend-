from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db
from app.models import OutboxMessage
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1", tags=["operations"])


class DeadLetterReplayIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/operations/dead-letters/{message_id}/replay")
async def replay_dead_letter(
    message_id: UUID,
    payload: DeadLetterReplayIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
) -> dict:
    """Requeue one terminal outbox message through the command boundary."""

    actor.require("integration.retry")

    async def action():
        item = await db.scalar(
            select(OutboxMessage)
            .where(
                OutboxMessage.id == message_id,
                OutboxMessage.tenant_id == actor.tenant_id,
            )
            .with_for_update()
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Dead letter not found."},
            )
        if item.status != "FAILED_TERMINAL":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DEAD_LETTER_NOT_REPLAYABLE",
                    "message": "Only terminally failed outbox messages may be replayed.",
                    "current_status": item.status,
                },
            )

        previous_attempts = item.attempts
        item.status = "PENDING_CONFIGURATION"
        item.attempts = 0
        response = {
            "replayed": True,
            "id": item.id,
            "previous_status": "FAILED_TERMINAL",
            "previous_attempts": previous_attempts,
            "status": item.status,
            "reason": payload.reason,
        }
        return response, "OutboxMessage", item.id, previous_attempts + 1

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="outbox.dead_letter.replay",
        payload={"message_id": str(message_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="operations.dead_letter.replayed.v1",
        audit_action="DEAD_LETTER_REPLAYED",
    )
