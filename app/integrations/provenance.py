from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.crypto import canonical_json, payload_hash, sha256_hex
from app.integrations.models import IntegrationProvenanceEntry

ZERO_HASH = "0" * 64


def _entity_uuid(*, tenant_id: UUID, stream: str, entity_id: str | UUID) -> UUID:
    """Normalize entity identifiers to the canonical UUID storage contract.

    Domain entities already use UUIDs. A deterministic UUIDv5 fallback keeps
    provenance append-only for provider identifiers without persisting raw values
    in a separate legacy table.
    """

    if isinstance(entity_id, UUID):
        return entity_id
    try:
        return UUID(str(entity_id))
    except ValueError:
        return uuid5(NAMESPACE_URL, f"freight:{tenant_id}:{stream}:{entity_id}")


def _record_material(
    *,
    tenant_id: UUID,
    chain_scope: str,
    sequence: int,
    previous_hash: str,
    event_type: str,
    entity_type: str,
    entity_id: UUID,
    payload_digest: str,
    correlation_id: str | None,
    metadata: dict[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "chain_scope": chain_scope,
        "sequence": sequence,
        "previous_hash": previous_hash,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "payload_hash": payload_digest,
        "correlation_id": correlation_id,
        "metadata": metadata,
        "created_at": created_at.isoformat(),
    }


async def append_provenance(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    stream: str,
    event_type: str,
    entity_id: str | UUID,
    payload: Any,
    actor_id: str,
    correlation_id: str,
    metadata: dict[str, Any] | None = None,
) -> IntegrationProvenanceEntry:
    """Append one tamper-evident record to a tenant-scoped hash chain.

    PostgreSQL advisory locking serializes writers for exactly one tenant/stream.
    No mutable chain-head row is required; the previous canonical entry is read
    under the same transaction after the lock is held.
    """

    if not stream or len(stream) > 180:
        raise ValueError("Provenance stream must contain 1 to 180 characters.")
    if not event_type or len(event_type) > 180:
        raise ValueError("Provenance event type must contain 1 to 180 characters.")
    if not actor_id or len(actor_id) > 220:
        raise ValueError("Provenance actor identifier is invalid.")
    if correlation_id and len(correlation_id) > 120:
        raise ValueError("Provenance correlation identifier exceeds 120 characters.")

    lock_key = f"{tenant_id}:{stream}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )

    previous = await db.scalar(
        select(IntegrationProvenanceEntry)
        .where(
            IntegrationProvenanceEntry.tenant_id == tenant_id,
            IntegrationProvenanceEntry.chain_scope == stream,
        )
        .order_by(IntegrationProvenanceEntry.sequence.desc())
        .limit(1)
    )

    created_at = datetime.now(timezone.utc)
    sequence = (previous.sequence if previous else 0) + 1
    previous_hash = previous.entry_hash if previous else ZERO_HASH
    stored_metadata = {
        **(metadata or {}),
        "actor_id": actor_id,
    }
    digest = payload_hash(payload)
    normalized_entity_id = _entity_uuid(
        tenant_id=tenant_id,
        stream=stream,
        entity_id=entity_id,
    )
    entity_type = str(stored_metadata.get("entity_type") or stream).split(":", 1)[0][
        :80
    ]
    material = _record_material(
        tenant_id=tenant_id,
        chain_scope=stream,
        sequence=sequence,
        previous_hash=previous_hash,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        payload_digest=digest,
        correlation_id=correlation_id or None,
        metadata=stored_metadata,
        created_at=created_at,
    )
    entry_hash = sha256_hex(canonical_json(material))
    record = IntegrationProvenanceEntry(
        tenant_id=tenant_id,
        chain_scope=stream,
        sequence=sequence,
        previous_hash=previous_hash,
        payload_hash=digest,
        entry_hash=entry_hash,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=normalized_entity_id,
        correlation_id=correlation_id or None,
        metadata_json=stored_metadata,
        created_at=created_at,
    )
    db.add(record)
    await db.flush()
    return record


async def verify_provenance_stream(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    stream: str,
) -> dict[str, Any]:
    records = (
        await db.scalars(
            select(IntegrationProvenanceEntry)
            .where(
                IntegrationProvenanceEntry.tenant_id == tenant_id,
                IntegrationProvenanceEntry.chain_scope == stream,
            )
            .order_by(IntegrationProvenanceEntry.sequence)
        )
    ).all()

    previous_hash = ZERO_HASH
    expected_sequence = 1
    for record in records:
        material = _record_material(
            tenant_id=record.tenant_id,
            chain_scope=record.chain_scope,
            sequence=record.sequence,
            previous_hash=record.previous_hash or ZERO_HASH,
            event_type=record.event_type,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            payload_digest=record.payload_hash,
            correlation_id=record.correlation_id,
            metadata=record.metadata_json,
            created_at=record.created_at,
        )
        calculated = sha256_hex(canonical_json(material))
        if (
            record.sequence != expected_sequence
            or (record.previous_hash or ZERO_HASH) != previous_hash
            or record.entry_hash != calculated
        ):
            return {
                "valid": False,
                "stream": stream,
                "failed_sequence": record.sequence,
                "records_checked": expected_sequence - 1,
            }
        previous_hash = record.entry_hash
        expected_sequence += 1

    return {
        "valid": True,
        "stream": stream,
        "records_checked": len(records),
        "head_hash": previous_hash,
    }
