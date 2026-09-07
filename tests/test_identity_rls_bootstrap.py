"""Identity lookup must establish RLS context before querying tenant grants."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app import security
from app.db import SessionLocal
from app.platform.models import ExternalIdentity, Membership, Principal, Tenant


@pytest.mark.asyncio
@pytest.mark.parametrize("has_membership", [True, False])
async def test_rls_context_precedes_membership_without_authorizing_the_selection(monkeypatch, has_membership):
    tenant_id, principal_id = uuid4(), uuid4()
    events = []
    identity = SimpleNamespace(last_seen_at=None)
    principal = SimpleNamespace(id=principal_id, status="ACTIVE", principal_type="USER")
    membership = SimpleNamespace(
        id=uuid4(), organization_id=None, customer_id=None, carrier_id=None,
    )

    async def install_context(db, tenant, actor):
        assert tenant == tenant_id
        assert actor == str(principal_id)
        events.append("context")

    async def read_membership(query):
        assert events == ["context"]
        events.append("membership")
        return membership if has_membership else None

    monkeypatch.setattr(security, "set_session_context", install_context)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(one_or_none=lambda: (identity, principal)),
            SimpleNamespace(all=lambda: [("reader", "customer.read")]),
        ]),
        scalar=AsyncMock(side_effect=read_membership),
    )
    claims = {"sub": "synthetic-subject", "iss": "https://identity.example.test",
              "permissions": ["*"], "realm_access": {"roles": ["admin"]}}
    settings = SimpleNamespace(oidc_issuer=claims["iss"])
    if has_membership:
        actor = await security._load_local_actor(db, claims, str(tenant_id), settings)
        assert actor.tenant_id == tenant_id
        assert actor.permissions == frozenset({"customer.read"})
        assert actor.roles == frozenset({"reader"})
        with pytest.raises(HTTPException):
            actor.require("customer.manage")
    else:
        with pytest.raises(HTTPException) as denied:
            await security._load_local_actor(db, claims, str(tenant_id), settings)
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == "TENANT_MEMBERSHIP_REQUIRED"
        assert db.execute.await_count == 1
    assert events == ["context", "membership"]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="requires disposable PostgreSQL")
async def test_local_identity_binding_under_real_non_bypass_rls():
    # Do not run this data fixture against a deployment database.
    assert os.environ.get("ENVIRONMENT") == "test"
    assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://freight:freight@localhost:5432/freight"
    tenant_id, other_tenant_id, principal_id = uuid4(), uuid4(), uuid4()
    issuer, subject = "https://identity.example.test/realms/rls", f"rls-{uuid4()}"
    async with SessionLocal() as db:
        db.add_all([
            Tenant(id=tenant_id, slug=f"test-{tenant_id}", name="Synthetic permitted tenant"),
            Tenant(id=other_tenant_id, slug=f"test-{other_tenant_id}", name="Synthetic denied tenant"),
            Principal(id=principal_id, display_name="Synthetic RLS principal", status="ACTIVE"),
        ])
        await db.flush()
        db.add_all([
            ExternalIdentity(principal_id=principal_id, issuer=issuer, subject=subject, enabled=True),
            Membership(tenant_id=tenant_id, principal_id=principal_id, status="ACTIVE"),
        ])
        await db.flush()
        await db.execute(text("SET LOCAL ROLE freight_api"))
        assert await db.scalar(text("SELECT current_user")) == "freight_api"
        role_flags = (await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ))).one()
        assert tuple(role_flags) == (False, False)
        claims = {"sub": subject, "iss": issuer}
        settings = SimpleNamespace(oidc_issuer=issuer)
        actor = await security._load_local_actor(db, claims, str(tenant_id), settings)
        assert actor.principal_id == principal_id
        assert actor.tenant_id == tenant_id
        assert not actor.permissions
        await db.flush()
        with pytest.raises(HTTPException) as denied:
            await security._load_local_actor(db, claims, str(other_tenant_id), settings)
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == "TENANT_MEMBERSHIP_REQUIRED"
        # All synthetic rows and SET LOCAL state are discarded together.
        await db.rollback()
