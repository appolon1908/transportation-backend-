from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.requests import Request

from app.db import SessionLocal, set_session_context
from app.integrations.models import IntegrationConnection, IntegrationWebhookKey
from app.integrations.security import signature_hex
from app.integrations.service import accept_inbound_webhook

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_AVAILABLE = os.getenv("DATABASE_URL", "").startswith("postgresql")


def test_release_topology_has_one_canonical_migration_chain() -> None:
    assert not (ROOT / "alembic-integrations.ini").exists()
    assert not (ROOT / "integration_migrations").exists()

    revisions = {
        path.name: path.read_text(encoding="utf-8")
        for path in (ROOT / "migrations" / "versions").glob("*.py")
    }
    assert "0003_integrations_durability.py" in revisions
    assert "0004_integration_rls_roles.py" in revisions
    assert "0005_portal_workflows.py" in revisions
    assert 'down_revision = "0003_integrations_durability"' in revisions[
        "0004_integration_rls_roles.py"
    ]
    assert 'down_revision = "0004_integration_rls_roles"' in revisions[
        "0005_portal_workflows.py"
    ]


def test_release_topology_has_separate_database_trust_roles() -> None:
    migration = (ROOT / "migrations" / "versions" / "0004_integration_rls_roles.py").read_text(
        encoding="utf-8"
    )
    for role in ("freight_api", "freight_ingress", "freight_worker"):
        assert f"CREATE ROLE {role} NOLOGIN" in migration
    assert migration.count("NOBYPASSRLS") >= 3
    assert "freight_resolve_webhook_connection" in migration
    assert "SECURITY DEFINER" in migration
    assert "REVOKE ALL ON TABLE integration_webhook_routes FROM PUBLIC" in migration


def test_production_runtime_registers_all_portal_surfaces() -> None:
    from app.production import app

    paths = set(app.openapi()["paths"])
    required = {
        "/api/v1/admin/portal-bindings",
        "/api/v1/admin/portal-reviews/claims",
        "/api/v1/operations/control-tower",
        "/api/v1/portals/customer/context",
        "/api/v1/portals/customer/quotes/{quote_id}/decision",
        "/api/v1/portals/carrier/context",
        "/api/v1/portals/carrier/tenders/{tender_id}/response",
        "/api/v1/portals/carrier/loads/{load_id}/tracking",
    }
    assert required <= paths


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL contract database is not configured")
@pytest.mark.asyncio
async def test_api_role_cannot_read_another_tenant_integration_rows() -> None:
    first_tenant = uuid4()
    second_tenant = uuid4()
    prefix = uuid4().hex

    async with SessionLocal() as db:
        db.add_all(
            [
                IntegrationConnection(
                    tenant_id=first_tenant,
                    name=f"rls-first-{prefix}",
                    kind="SIGNED_WEBHOOK",
                    webhook_slug=f"rls-first-{prefix}",
                    base_url="https://first.example.test",
                    enabled=False,
                ),
                IntegrationConnection(
                    tenant_id=second_tenant,
                    name=f"rls-second-{prefix}",
                    kind="SIGNED_WEBHOOK",
                    webhook_slug=f"rls-second-{prefix}",
                    base_url="https://second.example.test",
                    enabled=False,
                ),
            ]
        )
        await db.commit()

        await db.execute(text("SET LOCAL ROLE freight_api"))
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(first_tenant)},
        )
        visible = list(
            await db.scalars(
                select(IntegrationConnection.tenant_id).where(
                    IntegrationConnection.name.in_(
                        [f"rls-first-{prefix}", f"rls-second-{prefix}"]
                    )
                )
            )
        )
        assert visible == [first_tenant]
        await db.rollback()


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL contract database is not configured")
@pytest.mark.asyncio
async def test_ingress_role_resolves_only_opaque_enabled_webhook_route() -> None:
    tenant_id = uuid4()
    suffix = uuid4().hex
    slug = f"ingress-{suffix}"

    async with SessionLocal() as db:
        connection = IntegrationConnection(
            tenant_id=tenant_id,
            name=f"ingress-{suffix}",
            kind="SIGNED_WEBHOOK",
            webhook_slug=slug,
            base_url="https://ingress.example.test",
            enabled=True,
        )
        db.add(connection)
        await db.commit()
        await db.refresh(connection)

        privileges = (
            await db.execute(
                text(
                    """
                    SELECT
                        has_table_privilege(
                            'freight_ingress',
                            'public.integration_webhook_routes',
                            'SELECT'
                        ) AS can_enumerate,
                        has_function_privilege(
                            'freight_ingress',
                            'public.freight_resolve_webhook_connection(text)',
                            'EXECUTE'
                        ) AS can_resolve
                    """
                )
            )
        ).one()
        assert privileges.can_enumerate is False
        assert privileges.can_resolve is True

        await db.execute(text("SET LOCAL ROLE freight_ingress"))
        resolved = (
            await db.execute(
                text(
                    "SELECT connection_id, tenant_id "
                    "FROM public.freight_resolve_webhook_connection(:slug)"
                ),
                {"slug": slug},
            )
        ).one()
        assert resolved.connection_id == connection.id
        assert resolved.tenant_id == tenant_id

        await db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        visible_name = await db.scalar(
            select(IntegrationConnection.name).where(IntegrationConnection.id == connection.id)
        )
        assert visible_name == connection.name
        await db.rollback()


def _request(raw_body: bytes, *, correlation_id: str) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhook",
        "raw_path": b"/webhook",
        "query_string": b"",
        "headers": [(b"content-length", str(len(raw_body)).encode("ascii"))],
        "client": ("127.0.0.1", 12345),
        "server": ("api.example.test", 443),
    }
    request = Request(scope, receive)
    request.state.correlation_id = correlation_id
    return request


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="PostgreSQL contract database is not configured")
@pytest.mark.asyncio
async def test_signed_webhook_replay_is_idempotent_and_collision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    suffix = uuid4().hex
    secret = f"webhook-secret-{suffix}"
    secret_env = f"WEBHOOK_TEST_SECRET_{suffix.upper()}"
    monkeypatch.setenv(secret_env, secret)

    async with SessionLocal() as db:
        await set_session_context(db, tenant_id, "release-readiness-test")
        connection = IntegrationConnection(
            tenant_id=tenant_id,
            name=f"webhook-{suffix}",
            kind="SIGNED_WEBHOOK",
            webhook_slug=f"webhook-{suffix}",
            base_url="https://webhook.example.test",
            enabled=True,
        )
        db.add(connection)
        await db.flush()
        db.add(
            IntegrationWebhookKey(
                tenant_id=tenant_id,
                connection_id=connection.id,
                key_id="test-key",
                secret_ref=f"env:{secret_env}",
                active=True,
            )
        )
        await db.commit()

        timestamp = str(int(time.time()))
        event_id = f"event-{suffix}"
        first_body = json.dumps(
            {"type": "operations.exception.create", "data": {"code": "TEST_EVENT"}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        first_signature = signature_hex(secret, timestamp, first_body)

        accepted, duplicate = await accept_inbound_webhook(
            db,
            connection=connection,
            provider="release-test",
            request=_request(first_body, correlation_id=f"corr-{suffix}-1"),
            external_event_id=event_id,
            event_type_header=None,
            key_id="test-key",
            timestamp_header=timestamp,
            signature=first_signature,
        )
        assert duplicate is False

        replayed, duplicate = await accept_inbound_webhook(
            db,
            connection=connection,
            provider="release-test",
            request=_request(first_body, correlation_id=f"corr-{suffix}-2"),
            external_event_id=event_id,
            event_type_header=None,
            key_id="test-key",
            timestamp_header=timestamp,
            signature=first_signature,
        )
        assert duplicate is True
        assert replayed.id == accepted.id

        conflicting_body = json.dumps(
            {"type": "operations.exception.create", "data": {"code": "DIFFERENT_EVENT"}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        conflicting_signature = signature_hex(secret, timestamp, conflicting_body)
        with pytest.raises(HTTPException) as exc:
            await accept_inbound_webhook(
                db,
                connection=connection,
                provider="release-test",
                request=_request(conflicting_body, correlation_id=f"corr-{suffix}-3"),
                external_event_id=event_id,
                event_type_header=None,
                key_id="test-key",
                timestamp_header=timestamp,
                signature=conflicting_signature,
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "WEBHOOK_ID_PAYLOAD_CONFLICT"
