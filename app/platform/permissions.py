from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.models import Permission, Role, RolePermission


PERMISSIONS: dict[str, str] = {
    "customer.read": "Read customers, contacts and locations.",
    "customer.manage": "Create and update customers, contacts and locations.",
    "carrier.read": "Read carriers, equipment and compliance evidence.",
    "carrier.manage": "Create and update carriers and equipment.",
    "carrier.search": "Search eligible carriers for a load.",
    "carrier.compliance.manage": "Evaluate, approve or suspend carrier readiness.",
    "quote.read": "Read quotes and quote versions.",
    "quote.create": "Create and revise quotes.",
    "quote.send": "Send quotes to customers.",
    "quote.accept": "Accept or decline a scoped quote.",
    "shipment.read": "Read shipments, legs, stops and commodities.",
    "shipment.manage": "Create and change shipments.",
    "load.read": "Read loads and assignments.",
    "load.manage": "Create and plan loads.",
    "load.dispatch": "Dispatch and progress loads.",
    "tender.read": "Read tenders.",
    "tender.manage": "Create and withdraw tenders.",
    "tender.respond": "Accept or reject a scoped tender.",
    "tracking.read": "Read tracking and ETA data.",
    "tracking.manage": "Create authorized manual tracking events.",
    "document.read": "Read authorized document metadata and downloads.",
    "document.manage": "Create upload sessions and document versions.",
    "invoice.read": "Read customer invoices.",
    "invoice.manage": "Create, approve, issue or void invoices.",
    "settlement.read": "Read carrier settlements.",
    "settlement.manage": "Create, approve or void settlements.",
    "claim.read": "Read claims.",
    "claim.manage": "Create and manage claims.",
    "operations.read": "Read operations queues and dashboards.",
    "operations.manage": "Acknowledge, assign, resolve and override operations work.",
    "integration.read": "Read integration health and delivery status.",
    "integration.manage": "Configure integration connections and webhook keys.",
    "integration.retry": "Replay authorized inbox/outbox deliveries.",
    "compliance.read": "Read compliance policies and evaluations.",
    "compliance.manage": "Create policies and execute compliance evaluations.",
    "report.read": "Read tenant reports and exports.",
    "search.read": "Use tenant-scoped global search.",
    "admin.identity.read": "Read users, memberships, roles and permissions.",
    "admin.identity.manage": "Manage users, memberships, identities and roles.",
    "admin.capabilities.manage": "Manage tenant capability overrides.",
    "admin.audit.read": "Read tenant audit and provenance records.",
    "portal.customer": "Use customer portal resources scoped to the membership.",
    "portal.carrier": "Use carrier portal resources scoped to the membership.",
}

ROLE_TEMPLATES: dict[str, tuple[str, set[str]]] = {
    "admin": ("Tenant administrator", set(PERMISSIONS)),
    "operations": (
        "Operations supervisor",
        {
            "customer.read", "carrier.read", "carrier.search", "quote.read",
            "shipment.read", "shipment.manage", "load.read", "load.manage",
            "load.dispatch", "tender.read", "tender.manage", "tracking.read",
            "tracking.manage", "document.read", "document.manage", "operations.read",
            "operations.manage", "integration.read", "compliance.read", "report.read",
            "search.read",
        },
    ),
    "dispatcher": (
        "Dispatcher",
        {
            "customer.read", "carrier.read", "carrier.search", "quote.read",
            "shipment.read", "shipment.manage", "load.read", "load.manage",
            "load.dispatch", "tender.read", "tender.manage", "tracking.read",
            "tracking.manage", "document.read", "document.manage", "operations.read",
            "search.read",
        },
    ),
    "finance": (
        "Finance",
        {
            "customer.read", "carrier.read", "shipment.read", "load.read",
            "document.read", "invoice.read", "invoice.manage", "settlement.read",
            "settlement.manage", "claim.read", "claim.manage", "report.read",
            "search.read",
        },
    ),
    "customer": (
        "Customer portal user",
        {
            "portal.customer", "quote.read", "quote.accept", "shipment.read",
            "tracking.read", "document.read", "invoice.read", "report.read",
        },
    ),
    "carrier": (
        "Carrier portal user",
        {
            "portal.carrier", "tender.read", "tender.respond", "load.read",
            "tracking.read", "tracking.manage", "document.read", "document.manage",
            "settlement.read", "compliance.read",
        },
    ),
}


async def seed_permission_catalog(db: AsyncSession) -> dict[str, Permission]:
    existing = {item.code: item for item in (await db.scalars(select(Permission))).all()}
    for code, description in PERMISSIONS.items():
        item = existing.get(code)
        if item is None:
            item = Permission(code=code, description=description)
            db.add(item)
            existing[code] = item
        else:
            item.description = description
    await db.flush()
    return existing


async def seed_tenant_roles(db: AsyncSession, tenant_id: UUID) -> dict[str, Role]:
    permissions = await seed_permission_catalog(db)
    existing_roles = {
        item.code: item
        for item in (
            await db.scalars(select(Role).where(Role.tenant_id == tenant_id))
        ).all()
    }
    for code, (name, permission_codes) in ROLE_TEMPLATES.items():
        role = existing_roles.get(code)
        if role is None:
            role = Role(
                tenant_id=tenant_id,
                code=code,
                name=name,
                description=f"System role template: {name}",
                system=True,
            )
            db.add(role)
            await db.flush()
            existing_roles[code] = role
        await replace_role_permissions(db, role.id, permission_codes, permissions)
    return existing_roles


async def replace_role_permissions(
    db: AsyncSession,
    role_id: UUID,
    codes: Iterable[str],
    permission_map: dict[str, Permission] | None = None,
) -> None:
    permission_map = permission_map or await seed_permission_catalog(db)
    normalized = sorted(set(codes))
    unknown = [code for code in normalized if code not in permission_map]
    if unknown:
        raise ValueError(f"Unknown permissions: {', '.join(unknown)}")
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    for code in normalized:
        db.add(RolePermission(role_id=role_id, permission_id=permission_map[code].id))
    await db.flush()
