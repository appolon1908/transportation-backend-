"""Document APIs: durable metadata commands with quarantined, version-bound bytes."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.config import get_settings
from app.db import get_db
from app.documents.models import DocumentObject
from app.documents.schemas import AttachIn, DocumentOut, UploadIn, VersionIn
from app.documents.storage import (
    ObjectSpec, configured_settings, scanner_adapter, storage_adapter, unavailable,
)
from app.models import AuditEntry, Document, Load, LoadShipmentLeg, Shipment, ShipmentLeg
from app.portals.models import PortalPrincipalBinding
from app.portals.service import capability_is_enabled
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1", tags=["documents"])


def reject(status: int, code: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": "Document operation was not permitted."})


async def storage_access(actor: Actor = Depends(get_actor)):
    actor.require("document.manage")
    return configured_settings()


async def authorized_load(db: AsyncSession, actor: Actor, load_id: UUID) -> Load:
    load = await db.scalar(select(Load).where(Load.id == load_id, Load.tenant_id == actor.tenant_id))
    if load is None:
        raise reject(404, "NOT_FOUND")
    portal_kinds = []
    if actor.customer_id is not None or "portal.customer" in actor.permissions:
        portal_kinds.append(("CUSTOMER", actor.customer_id, "customer_portal.external_access"))
    if actor.carrier_id is not None or "portal.carrier" in actor.permissions:
        portal_kinds.append(("CARRIER", actor.carrier_id, "carrier_portal.external_access"))
    for kind, scope_id, capability in portal_kinds:
        actor.require("portal." + kind.lower())
        flag = getattr(get_settings(), f"capability_{kind.lower()}_portal_external_access")
        if scope_id is None or not flag or not await capability_is_enabled(db, actor.tenant_id, capability):
            raise reject(403, "PORTAL_CAPABILITY_DISABLED")
        binding = await db.scalar(select(PortalPrincipalBinding.id).where(
            PortalPrincipalBinding.tenant_id == actor.tenant_id,
            PortalPrincipalBinding.principal_issuer == actor.issuer.rstrip("/"),
            PortalPrincipalBinding.principal_subject == actor.subject,
            PortalPrincipalBinding.portal_kind == kind,
            PortalPrincipalBinding.resource_id == scope_id,
            PortalPrincipalBinding.status == "ACTIVE",
        ))
        if binding is None:
            raise reject(403, "PORTAL_PRINCIPAL_NOT_BOUND")
        if kind == "CARRIER" and load.carrier_id != scope_id:
            raise reject(404, "NOT_FOUND")
        if kind == "CUSTOMER":
            # A shared load must not disclose another customer's documents.
            customers = set(await db.scalars(select(Shipment.customer_id).distinct()
                .join(ShipmentLeg, ShipmentLeg.shipment_id == Shipment.id)
                .join(LoadShipmentLeg, LoadShipmentLeg.shipment_leg_id == ShipmentLeg.id)
                .where(LoadShipmentLeg.load_id == load_id,
                       LoadShipmentLeg.tenant_id == actor.tenant_id,
                       ShipmentLeg.tenant_id == actor.tenant_id,
                       Shipment.tenant_id == actor.tenant_id)))
            if customers != {scope_id}:
                raise reject(404, "NOT_FOUND")
    return load


def metadata(doc: Document, obj: DocumentObject | None) -> dict:
    values = {k: getattr(doc, k) for k in ("id", "load_id", "purpose", "status", "version", "checksum_sha256")}
    if obj:
        values.update(filename=obj.filename, content_type=obj.content_type, size_bytes=obj.size_bytes,
                      upload_expires_at=obj.expires_at, retain_until=obj.retain_until,
                      rejection_code=obj.rejection_code)
    return DocumentOut(**values).model_dump(mode="json")


async def document(db, actor, document_id, *, lock=False):
    query = select(Document, DocumentObject).join(DocumentObject, DocumentObject.document_id == Document.id).where(
        Document.id == document_id, Document.tenant_id == actor.tenant_id,
        DocumentObject.tenant_id == actor.tenant_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = (await db.execute(query)).one_or_none()
    if result is None or result[0].load_id is None:
        raise reject(404, "NOT_FOUND")
    await authorized_load(db, actor, result[0].load_id)
    return result


def spec_for(doc, obj) -> ObjectSpec:
    cfg = configured_settings()
    if obj.bucket != cfg.s3_bucket or not doc.object_key or not doc.checksum_sha256:
        raise unavailable("STORAGE_BINDING_MISMATCH")
    return ObjectSpec(doc.object_key, str(doc.tenant_id), str(doc.id), obj.content_type,
                      obj.size_bytes, doc.checksum_sha256, obj.retain_until)


async def command(db, request, actor, operation, payload, action):
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 200 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise reject(400, "IDEMPOTENCY_KEY_REQUIRED")
    # Serialize first-use races before querying the existing durable command record.
    digest = hashlib.sha256(f"{actor.tenant_id}:{actor.subject}:{operation}:{key}".encode()).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    return await execute_command(db=db, request=request, actor=actor, operation=operation,
        payload=payload, action=action, event_type=operation + ".v1", audit_action=operation.upper().replace(".", "_"))


async def provider_call(method, *args):
    try:
        return await asyncio.to_thread(method, *args)
    except HTTPException:
        raise
    except Exception as exc:
        # Provider exception messages can contain object coordinates; never expose them.
        raise unavailable() from exc


@router.post("/documents/upload-sessions")
async def create_upload_session(payload: UploadIn, request: Request, db: AsyncSession = Depends(get_db),
                                actor: Actor = Depends(get_actor), cfg=Depends(storage_access)):
    await authorized_load(db, actor, payload.load_id)
    if payload.size_bytes > cfg.max_bytes:
        raise reject(413, "DOCUMENT_TOO_LARGE")

    async def action():
        now = datetime.now(timezone.utc).replace(microsecond=0)
        doc_id = uuid4()
        doc = Document(id=doc_id, tenant_id=actor.tenant_id, load_id=payload.load_id,
                       purpose=payload.purpose, status="PENDING_UPLOAD", version=1,
                       object_key=f"tenants/{actor.tenant_id}/documents/{doc_id}", checksum_sha256=payload.checksum_sha256)
        obj = DocumentObject(document_id=doc_id, tenant_id=actor.tenant_id, created_by=actor.principal_id,
            filename=payload.filename, content_type=payload.content_type, size_bytes=payload.size_bytes,
            bucket=cfg.s3_bucket, expires_at=now + timedelta(seconds=cfg.upload_ttl_seconds),
            retain_until=now + timedelta(days=cfg.retention_days))
        db.add(doc)
        await db.flush()
        db.add(obj)
        await db.flush()
        return metadata(doc, obj), "Document", doc.id, doc.version

    result = await command(db, request, actor, "document.upload_requested", payload.model_dump(mode="json"), action)
    doc, obj = await document(db, actor, UUID(result["id"]))
    ttl = int((obj.expires_at - datetime.now(timezone.utc)).total_seconds())
    if ttl <= 0 or doc.status != "PENDING_UPLOAD":
        raise reject(409, "UPLOAD_SESSION_NOT_ACTIVE")
    # Signed URLs are generated AFTER commit and never stored in audit/outbox/idempotency.
    upload = await provider_call(storage_adapter().upload, spec_for(doc, obj), ttl)
    return {**metadata(doc, obj), "upload": upload}


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: UUID, db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("document.read")
    return metadata(*(await document(db, actor, document_id)))


@router.post("/documents/{document_id}/confirm", response_model=DocumentOut)
async def confirm_document(document_id: UUID, payload: VersionIn, request: Request,
                           db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor),
                           _cfg=Depends(storage_access)):
    await document(db, actor, document_id)

    async def action():
        doc, obj = await document(db, actor, document_id, lock=True)
        if doc.version != payload.expected_version:
            raise reject(409, "VERSION_CONFLICT")
        if doc.status != "PENDING_UPLOAD":
            raise reject(409, "DOCUMENT_NOT_CONFIRMABLE")
        if obj.expires_at <= datetime.now(timezone.utc):
            raise reject(410, "UPLOAD_SESSION_EXPIRED")
        spec = spec_for(doc, obj)
        store = storage_adapter()
        try:
            version = await asyncio.to_thread(store.inspect, spec)
            data = await asyncio.to_thread(store.read, spec, version)
        except ValueError:
            doc.status, obj.rejection_code = "REJECTED", "OBJECT_VALIDATION_FAILED"
        except Exception as exc:
            raise unavailable() from exc
        else:
            verdict = await scanner_adapter().scan(data)
            if verdict not in {"CLEAN", "INFECTED"}:
                raise unavailable("SCAN_FAILED")
            obj.version_id = version
            if verdict == "INFECTED":
                doc.status, obj.rejection_code = "QUARANTINED", "MALWARE_DETECTED"
            else:
                obj.verified_at = datetime.now(timezone.utc)
                doc.status = "AVAILABLE"
        doc.version += 1
        await db.flush()
        return metadata(doc, obj), "Document", doc.id, doc.version

    return await command(db, request, actor, "document.verification_completed",
                         {"document_id": str(document_id), **payload.model_dump()}, action)


@router.get("/loads/{load_id}/documents", response_model=list[DocumentOut])
async def load_documents(load_id: UUID, limit: int = Query(100, ge=1, le=200),
                         db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("document.read")
    await authorized_load(db, actor, load_id)
    rows = (await db.execute(select(Document, DocumentObject).outerjoin(DocumentObject,
        (DocumentObject.document_id == Document.id) & (DocumentObject.tenant_id == actor.tenant_id))
        .where(Document.tenant_id == actor.tenant_id, Document.load_id == load_id)
        .order_by(Document.created_at.desc(), Document.id.desc()).limit(limit))).all()
    return [metadata(doc, obj) for doc, obj in rows]


async def attach(load_id, payload, request, db, actor, *, pod):
    actor.require("document.manage")
    await authorized_load(db, actor, load_id)
    doc, _ = await document(db, actor, payload.document_id)
    if doc.load_id != load_id:
        raise reject(404, "NOT_FOUND")

    async def action():
        doc, obj = await document(db, actor, payload.document_id, lock=True)
        if doc.load_id != load_id:
            raise reject(404, "NOT_FOUND")
        if doc.version != payload.expected_version:
            raise reject(409, "VERSION_CONFLICT")
        if doc.status != "AVAILABLE" or not obj.verified_at or not obj.version_id:
            raise reject(409, "DOCUMENT_NOT_CLEAN")
        if obj.retain_until <= datetime.now(timezone.utc):
            raise reject(410, "DOCUMENT_EXPIRED")
        if pod and doc.purpose != "POD":
            raise reject(409, "DOCUMENT_PURPOSE_MISMATCH")
        doc.status, doc.version = "ATTACHED", doc.version + 1
        await db.flush()
        return metadata(doc, obj), "Document", doc.id, doc.version

    return await command(db, request, actor, "document.pod_attached" if pod else "document.attached",
        {"load_id": str(load_id), **payload.model_dump(mode="json")}, action)


@router.post("/loads/{load_id}/documents", response_model=DocumentOut)
async def attach_document(load_id: UUID, payload: AttachIn, request: Request,
                          db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor),
                          _cfg=Depends(storage_access)):
    return await attach(load_id, payload, request, db, actor, pod=False)


@router.post("/loads/{load_id}/pod", response_model=DocumentOut)
async def attach_pod(load_id: UUID, payload: AttachIn, request: Request,
                     db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor),
                     _cfg=Depends(storage_access)):
    return await attach(load_id, payload, request, db, actor, pod=True)


@router.get("/documents/{document_id}/download")
async def download_document(document_id: UUID, request: Request,
                            db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)):
    actor.require("document.read")
    doc, obj = await document(db, actor, document_id)
    if doc.status not in {"AVAILABLE", "ATTACHED"} or not obj.verified_at or not obj.version_id:
        raise reject(409, "DOCUMENT_NOT_CLEAN")
    if obj.retain_until <= datetime.now(timezone.utc):
        raise reject(410, "DOCUMENT_EXPIRED")
    data = await provider_call(storage_adapter().read, spec_for(doc, obj), obj.version_id)
    db.add(AuditEntry(tenant_id=actor.tenant_id, actor_id=actor.subject, action="DOCUMENT_DOWNLOADED",
        resource_type="Document", resource_id=doc.id, correlation_id=getattr(request.state, "correlation_id", ""),
        metadata_json={"checksum_sha256": doc.checksum_sha256}))
    await db.commit()
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{obj.filename}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
