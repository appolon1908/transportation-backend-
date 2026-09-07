"""Real identity/RBAC isolation plus forward-migration compatibility contracts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url

from app import security
from app.db import SessionLocal, set_session_context
from app.platform.models import (
    ExternalIdentity, Membership, MembershipRole, Organization, Permission,
    Principal, Role, RolePermission, Tenant,
)
from app.release import CANONICAL_MIGRATION_HEAD

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/versions/0006_identity_rbac_rls.py"
COMPLIANCE = ROOT / "compliance_migrations/versions/0001_carrier_readiness.py"
TABLES = (Organization, Role, Membership, RolePermission, MembershipRole)


def test_forward_revision_has_one_canonical_head_and_keeps_history():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["0006_identity_rbac_rls"]
    assert scripts.get_revision(CANONICAL_MIGRATION_HEAD).down_revision == "0005_portal_workflows"
    assert not (ROOT / "migrations/versions/0002b_identity_rbac_rls.py").exists()
    assert scripts.get_revision("0005_portal_workflows").down_revision == "0004_integration_rls_roles"


@pytest.mark.parametrize("has_membership", [True, False])
@pytest.mark.asyncio
async def test_context_is_installed_before_membership_without_granting_access(monkeypatch, has_membership):
    tenant_id, principal_id = uuid4(), uuid4()
    order = []
    principal = SimpleNamespace(id=principal_id, status="ACTIVE", principal_type="USER")
    identity = SimpleNamespace(last_seen_at=None)
    membership = SimpleNamespace(id=uuid4(), organization_id=None, customer_id=None, carrier_id=None)

    async def context(db, tenant, actor):
        assert (tenant, actor) == (tenant_id, str(principal_id))
        order.append("context")

    async def scalar(query):
        assert order == ["context"]
        order.append("membership")
        return membership if has_membership else None

    monkeypatch.setattr(security, "set_session_context", context)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(one_or_none=lambda: (identity, principal)),
            SimpleNamespace(all=lambda: [("reader", "customer.read")]),
        ]), scalar=AsyncMock(side_effect=scalar),
    )
    claims = {"sub": "test", "iss": "https://identity.example.test", "permissions": ["*"]}
    settings = SimpleNamespace(oidc_issuer=claims["iss"])
    if has_membership:
        actor = await security._load_local_actor(db, claims, str(tenant_id), settings)
        assert actor.permissions == frozenset({"customer.read"})
        with pytest.raises(HTTPException):
            actor.require("customer.manage")
    else:
        with pytest.raises(HTTPException) as denied:
            await security._load_local_actor(db, claims, str(tenant_id), settings)
        assert denied.value.detail["code"] == "TENANT_MEMBERSHIP_REQUIRED"
        assert db.execute.await_count == 1
    assert order == ["context", "membership"]


def test_published_compliance_migration_remains_byte_identical():
    assert hashlib.sha256(COMPLIANCE.read_bytes()).hexdigest() == "f1939a9ac9afc4151213c5db15dc2a624ac0d2f17c79c0cbb30f51fbeea4adfd"


def test_explicit_upgrade_orders_compliance_before_identity_hardening():
    from app.schema_upgrade import upgrade_plan
    assert upgrade_plan((), ()) == (
        ("alembic.ini", "0005_portal_workflows"),
        ("alembic-compliance.ini", "head"),
        ("alembic.ini", "head"),
    )
    for core in ("0005_portal_workflows", "0006_identity_rbac_rls"):
        assert upgrade_plan((core,), ("0001_carrier_readiness",)) == (
            ("alembic.ini", "head"), ("alembic-compliance.ini", "head"),
        )


@pytest.mark.parametrize("core,compliance", [
    (("unknown",), ()),
    (("0005_portal_workflows", "other"), ()),
    (("0005_portal_workflows",), ("unknown",)),
    ((), ("0001_carrier_readiness",)),
    (("0006_identity_rbac_rls",), ()),
])
def test_upgrade_refuses_unknown_split_or_out_of_order_history(core, compliance):
    from app.schema_upgrade import upgrade_plan
    with pytest.raises(ValueError):
        upgrade_plan(core, compliance)


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="requires disposable PostgreSQL")
@pytest.mark.asyncio
async def test_real_api_role_isolates_all_identity_grants_and_cross_role_links():
    url = make_url(os.environ["DATABASE_URL"])
    assert os.environ.get("ENVIRONMENT") == "test"
    assert url.host in {"localhost", "127.0.0.1"} and url.database in {"freight", "freight_test"}
    assert url.username == "freight" and url.port == 5432
    a, b, pa, pb = uuid4(), uuid4(), uuid4(), uuid4()
    issuer = "https://identity.example.test/realms/rls"
    subject = f"test-{uuid4()}"
    async with SessionLocal() as db:
        # Owner fixture preparation only. Every isolation assertion below uses
        # the application's actual non-owner, NOBYPASSRLS role.
        db.add_all([
            Tenant(id=a, slug=str(a), name="Synthetic A"),
            Tenant(id=b, slug=str(b), name="Synthetic B"),
            Principal(id=pa, display_name="Synthetic A", status="ACTIVE"),
            Principal(id=pb, display_name="Synthetic B", status="ACTIVE"),
        ])
        await db.flush()
        ra = Role(id=uuid4(), tenant_id=a, code="test-reader", name="Reader A")
        rb = Role(id=uuid4(), tenant_id=b, code="test-reader", name="Reader B")
        ma = Membership(id=uuid4(), tenant_id=a, principal_id=pa, status="ACTIVE")
        mb = Membership(id=uuid4(), tenant_id=b, principal_id=pb, status="ACTIVE")
        oa = Organization(id=uuid4(), tenant_id=a, name="Synthetic A", kind="BROKER")
        ob = Organization(id=uuid4(), tenant_id=b, name="Synthetic B", kind="BROKER")
        perm = Permission(id=uuid4(), code=f"test.{uuid4()}", description="Synthetic read")
        db.add_all([ra, rb, ma, mb, oa, ob, perm,
                    ExternalIdentity(principal_id=pa, issuer=issuer, subject=subject, enabled=True)])
        await db.flush()
        rpa = RolePermission(id=uuid4(), role_id=ra.id, permission_id=perm.id)
        rpb = RolePermission(id=uuid4(), role_id=rb.id, permission_id=perm.id)
        mra = MembershipRole(id=uuid4(), membership_id=ma.id, role_id=ra.id)
        mrb = MembershipRole(id=uuid4(), membership_id=mb.id, role_id=rb.id)
        db.add_all([rpa, rpb, mra, mrb])
        await db.flush()
        await db.execute(text("SET LOCAL ROLE freight_api"))
        flags = (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert tuple(flags) == (False, False)
        for model in TABLES:
            policy_flags = (await db.execute(text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:table)"
            ), {"table": "public." + model.__tablename__})).one()
            assert tuple(policy_flags) == (True, True)
        await db.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        for model in TABLES:
            assert not list(await db.scalars(select(model.id)))
        claims = {"sub": subject, "iss": issuer, "permissions": ["*"]}
        settings = SimpleNamespace(oidc_issuer=issuer)
        actor = await security._load_local_actor(db, claims, str(a), settings)
        assert actor.tenant_id == a and actor.principal_id == pa
        assert actor.permissions == frozenset({perm.code})
        for model, first, second in [(Organization, oa, ob), (Role, ra, rb), (Membership, ma, mb),
                                      (RolePermission, rpa, rpb), (MembershipRole, mra, mrb)]:
            assert list(await db.scalars(select(model.id).where(model.id.in_([first.id, second.id])))) == [first.id]
            assert (await db.execute(model.__table__.delete().where(model.id == second.id))).rowcount == 0
        # Test both foreign-key directions; an FK check alone can bypass RLS.
        for membership_id, role_id in ((ma.id, rb.id), (mb.id, ra.id)):
            with pytest.raises(DBAPIError):
                async with db.begin_nested():
                    await db.execute(MembershipRole.__table__.insert().values(
                        id=uuid4(), membership_id=membership_id, role_id=role_id,
                    ))
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(Role.__table__.insert().values(
                    id=uuid4(), tenant_id=b, code=f"denied-{uuid4()}", name="Must not exist",
                ))
        await db.flush()
        with pytest.raises(HTTPException) as denied:
            await security._load_local_actor(db, claims, str(b), settings)
        assert denied.value.detail["code"] == "TENANT_MEMBERSHIP_REQUIRED"
        await set_session_context(db, a, str(pa))
        assert await db.scalar(select(Membership.id).where(Membership.id == ma.id)) == ma.id
        await db.rollback()
