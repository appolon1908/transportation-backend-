"""Full HTTP/database document contracts; all S3 and antivirus effects are synthetic."""
from __future__ import annotations

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url

from app.documents import api
from app.documents.models import DocumentObject
from app.documents.storage import StorageSettings, validate_content
from app.db import SessionLocal, set_session_context
from app.models import AuditEntry, Document, IdempotencyRecord, Load, OutboxMessage
from app.production_v4 import app
from app.security import Actor

DATA = b"%PDF-1.4\nSynthetic test only.\n%%EOF"
DB = os.getenv("DATABASE_URL", "").startswith("postgresql")


def headers(tenant, key=None, permissions="document.read,document.manage"):
    result = {"X-Dev-Tenant-Id": str(tenant), "X-Dev-Actor": "document-contract-test",
              "X-Dev-Permissions": permissions}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


class FakeStore:
    data = DATA
    reads = 0
    uploads = 0
    def upload(self, spec, ttl):
        self.uploads += 1
        return {"url": "https://upload.example.invalid/object", "method": "PUT", "headers": {}}
    def inspect(self, spec):
        return "fixed-object-version"
    def read(self, spec, version):
        assert version == "fixed-object-version"
        self.reads += 1
        validate_content(self.data, spec)
        return self.data


class FakeScanner:
    verdict = "CLEAN"
    calls = 0
    async def scan(self, data):
        self.calls += 1
        if self.verdict == "ERROR":
            raise HTTPException(503, {"code": "SCAN_UNAVAILABLE"})
        return self.verdict


async def seed(tenant, load):
    assert os.environ.get("ENVIRONMENT") == "test"
    assert make_url(os.environ["DATABASE_URL"]).host in {"localhost", "127.0.0.1"}
    async with SessionLocal() as db:
        await set_session_context(db, tenant, "document-test-seed")
        db.add(Load(id=load, tenant_id=tenant, load_number=f"test-{load}"))
        await db.commit()


@pytest.fixture
def fixture(monkeypatch):
    tenant, load = uuid4(), uuid4()
    asyncio.run(seed(tenant, load))
    store, scanner = FakeStore(), FakeScanner()
    cfg = StorageSettings(storage_enabled=False, s3_bucket="test-documents")
    monkeypatch.setattr(api, "configured_settings", lambda: cfg)
    monkeypatch.setattr(api, "storage_adapter", lambda: store)
    monkeypatch.setattr(api, "scanner_adapter", lambda: scanner)
    body = {"load_id": str(load), "filename": "pod.pdf", "purpose": "POD", "content_type": "application/pdf",
            "size_bytes": len(DATA), "checksum_sha256": hashlib.sha256(DATA).hexdigest()}
    return SimpleNamespace(tenant=tenant, load=load, store=store, scanner=scanner, body=body, client=TestClient(app))


def create(f, key=None):
    response = f.client.post("/api/v1/documents/upload-sessions", json=f.body,
                             headers=headers(f.tenant, key or str(uuid4())))
    assert response.status_code == 200, response.text
    assert "object_key" not in response.json()
    return response.json()


def confirm(f, doc, key=None):
    return f.client.post(f"/api/v1/documents/{doc['id']}/confirm", json={"expected_version": 1},
                         headers=headers(f.tenant, key or str(uuid4())))


def test_every_document_route_is_bearer_authenticated():
    schema = app.openapi()
    for path, operations in schema["paths"].items():
        if "/documents" in path or path.endswith("/pod"):
            for method, operation in operations.items():
                if method in {"get", "post"}:
                    assert operation["security"] == [{"BearerAuth": []}]


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_upload_confirm_download_and_pod_are_idempotent_audited_and_evented(fixture):
    f = fixture
    doc = create(f, "upload")
    assert create(f, "upload")["id"] == doc["id"]
    denied = f.client.get(f"/api/v1/documents/{doc['id']}/download", headers=headers(f.tenant))
    assert denied.status_code == 409
    first = confirm(f, doc, "confirm")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "AVAILABLE"
    assert confirm(f, doc, "confirm").json() == first.json()
    assert f.scanner.calls == 1
    downloaded = f.client.get(f"/api/v1/documents/{doc['id']}/download", headers=headers(f.tenant))
    assert downloaded.status_code == 200 and downloaded.content == DATA
    assert downloaded.headers["content-disposition"] == 'attachment; filename="pod.pdf"'
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    attached = f.client.post(f"/api/v1/loads/{f.load}/pod", headers=headers(f.tenant, "pod"),
                            json={"document_id": doc["id"], "expected_version": 2})
    assert attached.status_code == 200, attached.text
    assert attached.json()["status"] == "ATTACHED"
    async def evidence():
        async with SessionLocal() as db:
            await set_session_context(db, f.tenant, "test")
            events = list(await db.scalars(select(OutboxMessage).where(OutboxMessage.tenant_id == f.tenant)))
            audits = list(await db.scalars(select(AuditEntry).where(AuditEntry.tenant_id == f.tenant)))
            records = list(await db.scalars(select(IdempotencyRecord).where(IdempotencyRecord.tenant_id == f.tenant)))
            assert len(events) == 3 and len(audits) == 4 and len(records) == 3
            assert all(event.status == "PENDING_CONFIGURATION" for event in events)
            assert "https://" not in str([e.payload for e in events])
            assert "upload.example" not in str([r.response_json for r in records])
    asyncio.run(evidence())


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
@pytest.mark.parametrize("verdict,status", [("INFECTED", "QUARANTINED"), ("ERROR", "PENDING_UPLOAD")])
def test_malware_and_unavailable_scanner_never_release_bytes(fixture, verdict, status):
    f = fixture
    doc = create(f)
    f.scanner.verdict = verdict
    result = confirm(f, doc)
    assert result.status_code == (503 if verdict == "ERROR" else 200)
    meta = f.client.get(f"/api/v1/documents/{doc['id']}", headers=headers(f.tenant))
    assert meta.json()["status"] == status
    assert f.client.get(f"/api/v1/documents/{doc['id']}/download", headers=headers(f.tenant)).status_code == 409
    assert f.client.post(f"/api/v1/loads/{f.load}/pod", headers=headers(f.tenant, "pod"),
        json={"document_id": doc["id"], "expected_version": meta.json()["version"]}).status_code == 409


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_changed_bytes_and_repeated_confirmation_fail_closed(fixture):
    f = fixture
    doc = create(f)
    f.store.data = b"altered"
    result = confirm(f, doc)
    assert result.json()["status"] == "REJECTED" and f.scanner.calls == 0
    f.store.data = DATA
    assert confirm(f, doc).status_code == 409


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_foreign_tenant_missing_permissions_and_conflicting_keys_are_denied(fixture):
    f = fixture
    doc = create(f, "same-key")
    bad = f.client.post("/api/v1/documents/upload-sessions", headers=headers(f.tenant, "same-key"),
                        json={**f.body, "filename": "different.pdf"})
    assert bad.status_code == 409
    for path in [f"/api/v1/documents/{doc['id']}", f"/api/v1/documents/{doc['id']}/download",
                 f"/api/v1/loads/{f.load}/documents"]:
        assert f.client.get(path, headers=headers(uuid4())).status_code == 404
        assert f.client.get(path, headers=headers(f.tenant, permissions="load.read")).status_code == 403
    assert f.store.reads == 0
    for key in [None, "x" * 201]:
        assert f.client.post("/api/v1/documents/upload-sessions", headers=headers(f.tenant, key), json=f.body).status_code == 400


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_concurrent_confirmation_is_not_scanned_or_evented_twice(fixture):
    f = fixture
    doc = create(f)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda k: confirm(f, doc, k).status_code, ["one", "two"]))
    assert sorted(responses) == [200, 409]
    assert f.scanner.calls == 1


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_expired_session_and_cross_load_attachment_are_denied(fixture):
    f = fixture
    doc = create(f)
    async def expire():
        async with SessionLocal() as db:
            await set_session_context(db, f.tenant, "test")
            obj = await db.get(DocumentObject, UUID(doc["id"]))
            obj.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
    asyncio.run(expire())
    assert confirm(f, doc).status_code == 410
    other = uuid4()
    asyncio.run(seed(f.tenant, other))
    assert f.client.post(f"/api/v1/loads/{other}/documents", headers=headers(f.tenant, "attach"),
        json={"document_id": doc["id"], "expected_version": 1}).status_code == 404


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_document_object_row_isolation_uses_non_bypass_api_role(fixture):
    f = fixture
    doc = create(f)
    async def check():
        async with SessionLocal() as db:
            await db.execute(text("SET LOCAL ROLE freight_api"))
            assert (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
            await db.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            assert await db.get(DocumentObject, UUID(doc["id"])) is None
            await set_session_context(db, uuid4(), "test")
            assert await db.get(DocumentObject, UUID(doc["id"])) is None
            await set_session_context(db, f.tenant, "test")
            assert await db.get(DocumentObject, UUID(doc["id"])) is not None
            with pytest.raises(DBAPIError):
                async with db.begin_nested():
                    await db.execute(text("UPDATE document_objects SET tenant_id=:other WHERE document_id=:id"),
                        {"other": uuid4(), "id": UUID(doc["id"])})
            await db.rollback()
    asyncio.run(check())


@pytest.mark.asyncio
async def test_unbound_portal_actor_cannot_use_generic_document_routes(monkeypatch):
    actor = Actor(subject="test", issuer="issuer", principal_id=uuid4(), membership_id=uuid4(),
        tenant_id=uuid4(), organization_id=None, customer_id=None, carrier_id=None,
        principal_type="USER", permissions=frozenset({"document.read", "portal.carrier"}), roles=frozenset())
    async def scalar(query):
        return SimpleNamespace(carrier_id=uuid4())
    with pytest.raises(HTTPException) as denied:
        await api.authorized_load(SimpleNamespace(scalar=scalar), actor, uuid4())
    assert denied.value.status_code == 403


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_scanning_releases_request_transaction_and_commits_claim(fixture):
    from app.db import get_db
    f = fixture
    sessions = []

    async def tracked_db():
        async with SessionLocal() as session:
            sessions.append(session)
            yield session
            if session.in_transaction():
                await session.rollback()

    async def scan(data):
        assert all(not session.in_transaction() for session in sessions)
        async with SessionLocal() as session:
            await set_session_context(session, f.tenant, "verify-claim")
            doc = await session.scalar(select(Document).where(Document.load_id == f.load).with_for_update(nowait=True))
            assert doc.status == "VERIFYING"
            obj = await session.get(DocumentObject, doc.id)
            assert obj.verification_token is not None
            assert obj.verification_expires_at > datetime.now(timezone.utc)
        return "CLEAN"

    f.scanner.scan = scan
    app.dependency_overrides[get_db] = tracked_db
    try:
        doc = create(f)
        result = confirm(f, doc, "claim-test")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "AVAILABLE"
        assert confirm(f, doc, "claim-test").json() == result.json()
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_expired_verification_claim_can_be_retried(fixture):
    f = fixture
    doc = create(f)

    async def abandoned_claim():
        async with SessionLocal() as session:
            await set_session_context(session, f.tenant, "abandoned-claim")
            item = await session.get(Document, UUID(doc["id"]))
            obj = await session.get(DocumentObject, item.id)
            item.status = "VERIFYING"
            obj.verification_token = uuid4()
            obj.verification_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(abandoned_claim())
    result = confirm(f, doc)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "AVAILABLE"


@pytest.mark.skipif(not DB, reason="requires disposable PostgreSQL")
def test_replaced_claim_cannot_publish_or_clear_the_new_claim(fixture):
    f = fixture
    doc = create(f)
    replacement = uuid4()

    async def scan(data):
        async with SessionLocal() as session:
            await set_session_context(session, f.tenant, "replacement-claim")
            obj = await session.get(DocumentObject, UUID(doc["id"]))
            obj.verification_token = replacement
            obj.verification_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            await session.commit()
        return "CLEAN"

    f.scanner.scan = scan
    assert confirm(f, doc).status_code == 409

    async def check():
        async with SessionLocal() as session:
            await set_session_context(session, f.tenant, "check-claim")
            item = await session.get(Document, UUID(doc["id"]))
            obj = await session.get(DocumentObject, item.id)
            assert item.status == "VERIFYING"
            assert obj.verification_token == replacement
            assert obj.verified_at is None
    asyncio.run(check())
