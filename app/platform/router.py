from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands import execute_command
from app.db import get_db
from app.models import AuditEntry, Capability
from app.platform.models import (
    ExternalIdentity,
    Membership,
    MembershipRole,
    Organization,
    Permission,
    Principal,
    Role,
    RolePermission,
    Tenant,
)
from app.platform.permissions import PERMISSIONS, replace_role_permissions
from app.security import Actor, get_actor

router = APIRouter(prefix="/api/v1", tags=["identity-and-tenancy"])


def model_row(model: Any) -> dict[str, Any]:
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}


async def tenant_resource(db: AsyncSession, model: type, item_id: UUID, actor: Actor):
    item = await db.scalar(
        select(model).where(model.id == item_id, model.tenant_id == actor.tenant_id)
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Resource not found."},
        )
    return item


class TenantPatch(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    expected_version: int = Field(ge=1)


class OrganizationIn(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    kind: str = Field(pattern="^(BROKER|CUSTOMER|CARRIER|INTERNAL)$")
    external_reference: str | None = Field(default=None, max_length=160)


class PrincipalIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=250)
    email: str | None = Field(default=None, max_length=320)
    principal_type: str = Field(default="USER", pattern="^(USER|SERVICE)$")
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=255)


class IdentityIn(BaseModel):
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=255)
    identity_type: str = Field(default="OIDC", max_length=32)


class MembershipIn(BaseModel):
    principal_id: UUID
    organization_id: UUID | None = None
    customer_id: UUID | None = None
    carrier_id: UUID | None = None
    role_codes: list[str] = Field(default_factory=list, max_length=20)


class RoleIn(BaseModel):
    code: str = Field(min_length=2, max_length=100, pattern="^[a-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] = Field(default_factory=list)


class RoleSet(BaseModel):
    role_codes: list[str] = Field(default_factory=list, max_length=20)


class PermissionSet(BaseModel):
    permission_codes: list[str] = Field(default_factory=list)


class CapabilityPatch(BaseModel):
    enabled: bool


@router.get("/auth/context")
async def auth_context(actor: Actor = Depends(get_actor)):
    return {
        "subject": actor.subject,
        "issuer": actor.issuer,
        "principal_id": actor.principal_id,
        "membership_id": actor.membership_id,
        "tenant_id": actor.tenant_id,
        "organization_id": actor.organization_id,
        "customer_id": actor.customer_id,
        "carrier_id": actor.carrier_id,
        "principal_type": actor.principal_type,
        "roles": sorted(actor.roles),
        "permissions": sorted(actor.permissions),
    }


@router.get("/admin/tenant")
async def current_tenant(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
):
    actor.require("admin.identity.read")
    item = await db.scalar(select(Tenant).where(Tenant.id == actor.tenant_id))
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "TENANT_NOT_BOOTSTRAPPED",
                "message": "Tenant is not bootstrapped.",
            },
        )
    return model_row(item)


@router.patch("/admin/tenant")
async def update_tenant(
    payload: TenantPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        item = await db.scalar(select(Tenant).where(Tenant.id == actor.tenant_id))
        if item is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Tenant not found."},
            )
        if item.version != payload.expected_version:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "STALE_VERSION",
                    "message": "Tenant version is stale.",
                    "current_version": item.version,
                },
            )
        item.name = payload.name.strip()
        item.version += 1
        return model_row(item), "Tenant", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="tenant.update",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="tenant.updated.v1",
        audit_action="TENANT_UPDATED",
    )


@router.get("/admin/organizations")
async def list_organizations(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
):
    actor.require("admin.identity.read")
    items = (
        await db.scalars(
            select(Organization)
            .where(Organization.tenant_id == actor.tenant_id)
            .order_by(Organization.kind, Organization.name)
        )
    ).all()
    return [model_row(item) for item in items]


@router.post("/admin/organizations", status_code=201)
async def create_organization(
    payload: OrganizationIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        item = Organization(
            tenant_id=actor.tenant_id,
            name=payload.name.strip(),
            kind=payload.kind,
            external_reference=payload.external_reference,
        )
        db.add(item)
        await db.flush()
        return model_row(item), "Organization", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="organization.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="organization.created.v1",
        audit_action="ORGANIZATION_CREATED",
    )


@router.get("/admin/users")
async def list_users(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
):
    actor.require("admin.identity.read")
    rows = (
        await db.execute(
            select(Principal, Membership)
            .join(Membership, Membership.principal_id == Principal.id)
            .where(Membership.tenant_id == actor.tenant_id)
            .order_by(Principal.display_name)
        )
    ).all()
    return [
        {"principal": model_row(principal), "membership": model_row(membership)}
        for principal, membership in rows
    ]


@router.post("/admin/users", status_code=201)
async def create_user(
    payload: PrincipalIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        existing = await db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.issuer == payload.issuer.rstrip("/"),
                ExternalIdentity.subject == payload.subject,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDENTITY_ALREADY_BOUND",
                    "message": "Identity is already bound.",
                },
            )
        principal = Principal(
            display_name=payload.display_name.strip(),
            email=payload.email.lower() if payload.email else None,
            principal_type=payload.principal_type,
        )
        db.add(principal)
        await db.flush()
        db.add(
            ExternalIdentity(
                principal_id=principal.id,
                issuer=payload.issuer.rstrip("/"),
                subject=payload.subject,
            )
        )
        return model_row(principal), "Principal", principal.id, principal.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="principal.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="principal.created.v1",
        audit_action="PRINCIPAL_CREATED",
    )


@router.post("/admin/users/{principal_id}/identities", status_code=201)
async def bind_identity(
    principal_id: UUID,
    payload: IdentityIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        membership = await db.scalar(
            select(Membership).where(
                Membership.tenant_id == actor.tenant_id,
                Membership.principal_id == principal_id,
            )
        )
        if membership is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "User not found."},
            )
        item = ExternalIdentity(
            principal_id=principal_id,
            issuer=payload.issuer.rstrip("/"),
            subject=payload.subject,
            identity_type=payload.identity_type,
        )
        db.add(item)
        await db.flush()
        return model_row(item), "ExternalIdentity", item.id, 1

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="identity.bind",
        payload={"principal_id": str(principal_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="identity.bound.v1",
        audit_action="IDENTITY_BOUND",
    )


@router.get("/admin/memberships")
async def list_memberships(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
):
    actor.require("admin.identity.read")
    items = (
        await db.scalars(
            select(Membership)
            .where(Membership.tenant_id == actor.tenant_id)
            .order_by(Membership.created_at)
        )
    ).all()
    return [model_row(item) for item in items]


@router.post("/admin/memberships", status_code=201)
async def create_membership(
    payload: MembershipIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        principal = await db.scalar(
            select(Principal).where(Principal.id == payload.principal_id)
        )
        if principal is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Principal not found."},
            )
        item = Membership(
            tenant_id=actor.tenant_id,
            principal_id=payload.principal_id,
            organization_id=payload.organization_id,
            customer_id=payload.customer_id,
            carrier_id=payload.carrier_id,
        )
        db.add(item)
        await db.flush()
        role_codes = sorted(set(payload.role_codes))
        roles = (
            await db.scalars(
                select(Role).where(
                    Role.tenant_id == actor.tenant_id,
                    Role.code.in_(role_codes),
                )
            )
        ).all()
        if len(roles) != len(role_codes):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UNKNOWN_ROLE",
                    "message": "One or more roles are unknown.",
                },
            )
        for role in roles:
            db.add(MembershipRole(membership_id=item.id, role_id=role.id))
        return model_row(item), "Membership", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="membership.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="membership.created.v1",
        audit_action="MEMBERSHIP_CREATED",
    )


@router.put("/admin/memberships/{membership_id}/roles")
async def set_membership_roles(
    membership_id: UUID,
    payload: RoleSet,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        item = await tenant_resource(db, Membership, membership_id, actor)
        role_codes = sorted(set(payload.role_codes))
        roles = (
            await db.scalars(
                select(Role).where(
                    Role.tenant_id == actor.tenant_id,
                    Role.code.in_(role_codes),
                )
            )
        ).all()
        if len(roles) != len(role_codes):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UNKNOWN_ROLE",
                    "message": "One or more roles are unknown.",
                },
            )
        await db.execute(
            delete(MembershipRole).where(MembershipRole.membership_id == item.id)
        )
        for role in roles:
            db.add(MembershipRole(membership_id=item.id, role_id=role.id))
        item.version += 1
        return {
            "membership_id": item.id,
            "roles": role_codes,
            "version": item.version,
        }, "Membership", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="membership.roles.replace",
        payload={"membership_id": str(membership_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="membership.roles.changed.v1",
        audit_action="MEMBERSHIP_ROLES_CHANGED",
    )


@router.get("/admin/roles")
async def list_roles(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
):
    actor.require("admin.identity.read")
    roles = (
        await db.scalars(
            select(Role).where(Role.tenant_id == actor.tenant_id).order_by(Role.code)
        )
    ).all()
    result = []
    for role in roles:
        codes = (
            await db.scalars(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
                .order_by(Permission.code)
            )
        ).all()
        result.append({**model_row(role), "permissions": list(codes)})
    return result


@router.post("/admin/roles", status_code=201)
async def create_role(
    payload: RoleIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        item = Role(
            tenant_id=actor.tenant_id,
            code=payload.code,
            name=payload.name,
            description=payload.description,
            system=False,
        )
        db.add(item)
        await db.flush()
        try:
            await replace_role_permissions(db, item.id, payload.permissions)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "UNKNOWN_PERMISSION", "message": str(exc)},
            ) from exc
        return model_row(item), "Role", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="role.create",
        payload=payload.model_dump(mode="json"),
        action=action,
        event_type="role.created.v1",
        audit_action="ROLE_CREATED",
    )


@router.put("/admin/roles/{role_id}/permissions")
async def set_role_permissions(
    role_id: UUID,
    payload: PermissionSet,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.identity.manage")

    async def action():
        item = await tenant_resource(db, Role, role_id, actor)
        try:
            await replace_role_permissions(db, item.id, payload.permission_codes)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "UNKNOWN_PERMISSION", "message": str(exc)},
            ) from exc
        item.version += 1
        return {
            "role_id": item.id,
            "permissions": sorted(set(payload.permission_codes)),
            "version": item.version,
        }, "Role", item.id, item.version

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="role.permissions.replace",
        payload={"role_id": str(role_id), **payload.model_dump(mode="json")},
        action=action,
        event_type="role.permissions.changed.v1",
        audit_action="ROLE_PERMISSIONS_CHANGED",
    )


@router.get("/admin/permissions")
async def list_permissions(actor: Actor = Depends(get_actor)):
    actor.require("admin.identity.read")
    return [
        {"code": code, "description": description}
        for code, description in sorted(PERMISSIONS.items())
    ]


@router.get("/admin/capabilities")
async def list_admin_capabilities(
    db: AsyncSession = Depends(get_db), actor: Actor = Depends(get_actor)
):
    actor.require("admin.capabilities.manage")
    items = (
        await db.scalars(
            select(Capability)
            .where(Capability.tenant_id == actor.tenant_id)
            .order_by(Capability.code)
        )
    ).all()
    return [model_row(item) for item in items]


@router.patch("/admin/capabilities/{code}")
async def update_admin_capability(
    code: str,
    payload: CapabilityPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.capabilities.manage")

    async def action():
        item = await db.scalar(
            select(Capability).where(
                Capability.tenant_id == actor.tenant_id,
                Capability.code == code,
            )
        )
        if item is None:
            item = Capability(tenant_id=actor.tenant_id, code=code, enabled=False)
            db.add(item)
            await db.flush()
        item.enabled = payload.enabled
        return model_row(item), "Capability", item.id, 1

    return await execute_command(
        db=db,
        request=request,
        actor=actor,
        operation="capability.update",
        payload={"code": code, **payload.model_dump(mode="json")},
        action=action,
        event_type="capability.changed.v1",
        audit_action="CAPABILITY_CHANGED",
    )


@router.get("/admin/audit")
async def list_audit(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    actor.require("admin.audit.read")
    bounded_limit = max(1, min(limit, 500))
    items = (
        await db.scalars(
            select(AuditEntry)
            .where(AuditEntry.tenant_id == actor.tenant_id)
            .order_by(AuditEntry.created_at.desc())
            .limit(bounded_limit)
        )
    ).all()
    return [model_row(item) for item in items]
