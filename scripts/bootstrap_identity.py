from __future__ import annotations

import argparse
import asyncio
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db import SessionLocal
from app.platform.models import (
    ExternalIdentity,
    Membership,
    MembershipRole,
    Principal,
    Tenant,
)
from app.platform.permissions import seed_tenant_roles


async def bootstrap(args: argparse.Namespace) -> None:
    async with SessionLocal() as db:
        tenant = await db.scalar(select(Tenant).where(Tenant.slug == args.tenant_slug))
        if tenant is None:
            tenant = Tenant(
                id=UUID(args.tenant_id) if args.tenant_id else uuid4(),
                slug=args.tenant_slug,
                name=args.tenant_name,
            )
            db.add(tenant)
            await db.flush()

        identity = await db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.issuer == args.issuer.rstrip("/"),
                ExternalIdentity.subject == args.subject,
            )
        )
        if identity is None:
            principal = Principal(
                display_name=args.display_name,
                email=args.email.lower() if args.email else None,
                principal_type="USER",
            )
            db.add(principal)
            await db.flush()
            identity = ExternalIdentity(
                principal_id=principal.id,
                issuer=args.issuer.rstrip("/"),
                subject=args.subject,
            )
            db.add(identity)
        else:
            principal = await db.get(Principal, identity.principal_id)
            if principal is None:
                raise RuntimeError("External identity references a missing principal")

        membership = await db.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant.id,
                Membership.principal_id == principal.id,
            )
        )
        if membership is None:
            membership = Membership(tenant_id=tenant.id, principal_id=principal.id)
            db.add(membership)
            await db.flush()

        roles = await seed_tenant_roles(db, tenant.id)
        admin_role = roles["admin"]
        assignment = await db.scalar(
            select(MembershipRole).where(
                MembershipRole.membership_id == membership.id,
                MembershipRole.role_id == admin_role.id,
            )
        )
        if assignment is None:
            db.add(MembershipRole(membership_id=membership.id, role_id=admin_role.id))

        await db.commit()
        print(f"TENANT_ID={tenant.id}")
        print(f"PRINCIPAL_ID={principal.id}")
        print(f"MEMBERSHIP_ID={membership.id}")
        print("ROLE=admin")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap the first tenant administrator"
    )
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--email", default="")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(bootstrap(parse_args()))
