from __future__ import annotations

import os
from argparse import Namespace
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import SessionLocal, set_session_context
from app.platform.models import (
    ExternalIdentity, Membership, MembershipRole, Organization, Permission,
    Principal, Role, RolePermission, Tenant,
)
from app.security import _load_local_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL contract database is not configured",
)
SCOPED = (Organization, Role, Membership, RolePermission, MembershipRole)


@pytest.fixture
async def identity_rows():
    url = make_url(os.environ["DATABASE_URL"])
    assert os.environ.get("ENVIRONMENT") == "test"
    assert url.host in {"localhost", "127.0.0.1"} and url.port == 5432
    assert url.database in {"freight", "freight_test"} and url.username == "freight"
    suffix = uuid4().hex
    result = []
    async with SessionLocal() as db:
        for index in range(2):
            tenant = Tenant(slug=f"identity-rls-{suffix}-{index}", name="Test tenant")
            principal = Principal(display_name="Test principal")
            permission = Permission(code=f"test.identity.{suffix}.{index}", description="Test grant")
            db.add_all([tenant, principal, permission])
            await db.flush()
            await set_session_context(db, tenant.id, str(principal.id))
            organization = Organization(tenant_id=tenant.id, name="Test organization", kind="INTERNAL")
            role = Role(tenant_id=tenant.id, code="test-reader", name="Test reader")
            membership = Membership(tenant_id=tenant.id, principal_id=principal.id)
            identity = ExternalIdentity(
                principal_id=principal.id, issuer=get_settings().oidc_issuer.rstrip("/"),
                subject=f"test-subject-{suffix}-{index}",
            )
            db.add_all([organization, role, membership, identity])
            await db.flush()
            grant = RolePermission(role_id=role.id, permission_id=permission.id)
            assignment = MembershipRole(membership_id=membership.id, role_id=role.id)
            db.add_all([grant, assignment])
            await db.flush()
            result.append({
                "tenant": tenant, "principal": principal, "permission": permission,
                "identity": identity,
                Organization: organization, Role: role, Membership: membership,
                RolePermission: grant, MembershipRole: assignment,
            })
        await db.commit()
    return result


async def use_api_role(db, tenant_id=None):
    await db.execute(text("SET LOCAL ROLE freight_api"))
    if tenant_id is not None:
        await set_session_context(db, tenant_id, "identity-rls-test")
    else:
        await db.execute(text("SELECT set_config('app.tenant_id', '', true)"))


async def test_identity_tables_force_rls_for_nonowner_api_role():
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
            WHERE oid IN (
                'public.platform_organizations'::regclass, 'public.platform_roles'::regclass,
                'public.platform_memberships'::regclass, 'public.platform_role_permissions'::regclass,
                'public.platform_membership_roles'::regclass
            )
        """))).all()
        assert len(rows) == 5
        assert all(enabled and forced for _, enabled, forced in rows)
        unsafe = await db.scalar(text(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'freight_api'"
        ))
        assert unsafe is False


@pytest.mark.parametrize("model", SCOPED)
async def test_identity_rows_require_context_and_hide_other_tenants(identity_rows, model):
    first, second = identity_rows
    ids = [first[model].id, second[model].id]
    async with SessionLocal() as db:
        await use_api_role(db)
        assert list(await db.scalars(select(model.id).where(model.id.in_(ids)))) == []
        await set_session_context(db, first["tenant"].id, "test")
        assert list(await db.scalars(select(model.id).where(model.id.in_(ids)))) == [ids[0]]
        await db.commit()
        # SET LOCAL ROLE resets on commit; request context must be restored by
        # after_begin while the API test role is reselected for this transaction.
        await db.execute(text("SET LOCAL ROLE freight_api"))
        assert list(await db.scalars(select(model.id).where(model.id.in_(ids)))) == [ids[0]]


@pytest.mark.parametrize("model", SCOPED)
async def test_identity_cross_tenant_update_and_delete_touch_no_rows(identity_rows, model):
    first, second = identity_rows
    async with SessionLocal() as db:
        await use_api_role(db, first["tenant"].id)
        changed = await db.execute(
            update(model).where(model.id == second[model].id)
            .values(id=second[model].id).returning(model.id)
        )
        assert changed.all() == []
        removed = await db.execute(
            delete(model).where(model.id == second[model].id).returning(model.id)
        )
        assert removed.all() == []


@pytest.mark.parametrize("model", SCOPED)
async def test_identity_cross_tenant_insert_is_rejected(identity_rows, model):
    first, second = identity_rows
    values = {
        Organization: dict(tenant_id=second["tenant"].id, name=uuid4().hex, kind="INTERNAL"),
        Role: dict(tenant_id=second["tenant"].id, code=uuid4().hex, name="Denied"),
        Membership: dict(tenant_id=second["tenant"].id, principal_id=first["principal"].id),
        RolePermission: dict(role_id=second[Role].id, permission_id=first["permission"].id),
        MembershipRole: dict(membership_id=second[Membership].id, role_id=first[Role].id),
    }
    async with SessionLocal() as db:
        await use_api_role(db, first["tenant"].id)
        db.add(model(**values[model]))
        with pytest.raises(DBAPIError) as denied:
            await db.flush()
        assert getattr(denied.value.orig, "sqlstate", None) == "42501", str(denied.value)
        await db.rollback()


async def test_membership_assignment_checks_role_parent_tenant_too(identity_rows):
    first, second = identity_rows
    async with SessionLocal() as db:
        await use_api_role(db, first["tenant"].id)
        db.add(MembershipRole(membership_id=first[Membership].id, role_id=second[Role].id))
        with pytest.raises(DBAPIError) as denied:
            await db.flush()
        assert getattr(denied.value.orig, "sqlstate", None) == "42501"
        await db.rollback()


async def test_local_actor_resolves_membership_and_only_database_granted_permissions(identity_rows):
    first, second = identity_rows
    claims = {
        "sub": first["identity"].subject, "iss": first["identity"].issuer,
        "roles": ["admin"], "permissions": ["*"],
    }
    async with SessionLocal() as db:
        await use_api_role(db)
        actor = await _load_local_actor(db, claims, str(first["tenant"].id), get_settings())
        assert actor.principal_id == first["principal"].id
        assert actor.membership_id == first[Membership].id
        assert actor.permissions == frozenset({first["permission"].code})
        assert actor.roles == frozenset({first[Role].code})
        with pytest.raises(HTTPException) as denied:
            await _load_local_actor(db, claims, str(second["tenant"].id), get_settings())
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == "TENANT_MEMBERSHIP_REQUIRED"


async def test_disabled_membership_cannot_authenticate(identity_rows):
    first = identity_rows[0]
    async with SessionLocal() as db:
        await db.execute(update(Membership).where(Membership.id == first[Membership].id).values(status="DISABLED"))
        await db.commit()
        await use_api_role(db)
        with pytest.raises(HTTPException) as denied:
            await _load_local_actor(db, {
                "sub": first["identity"].subject, "iss": first["identity"].issuer,
            }, str(first["tenant"].id), get_settings())
        assert denied.value.detail["code"] == "TENANT_MEMBERSHIP_REQUIRED"


async def test_bootstrap_is_repeatable_with_nonowner_rls_role(monkeypatch):
    from scripts import bootstrap_identity

    @asynccontextmanager
    async def api_session():
        async with SessionLocal() as db:
            await db.execute(text("SET LOCAL ROLE freight_api"))
            yield db

    monkeypatch.setattr(bootstrap_identity, "SessionLocal", api_session)
    url = make_url(os.environ["DATABASE_URL"])
    assert os.environ.get("ENVIRONMENT") == "test"
    assert url.host in {"localhost", "127.0.0.1"} and url.port == 5432
    assert url.database in {"freight", "freight_test"} and url.username == "freight"
    suffix = uuid4().hex
    args = Namespace(
        tenant_id=str(uuid4()), tenant_slug=f"bootstrap-{suffix}", tenant_name="Test",
        issuer=get_settings().oidc_issuer, subject=f"bootstrap-{suffix}",
        display_name="Test bootstrap administrator", email="",
    )
    await bootstrap_identity.bootstrap(args)
    await bootstrap_identity.bootstrap(args)
    async with SessionLocal() as db:
        count = await db.scalar(select(func.count(Membership.id)).where(Membership.tenant_id == args.tenant_id))
        assert count == 1
        await use_api_role(db)
        actor = await _load_local_actor(db, {"iss": args.issuer, "sub": args.subject}, args.tenant_id, get_settings())
        assert "admin" in actor.roles
        assert "admin.identity.manage" in actor.permissions
