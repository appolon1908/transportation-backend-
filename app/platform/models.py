from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "platform_tenants"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(250))
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Organization(Base):
    __tablename__ = "platform_organizations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "kind", name="uq_platform_organization_name"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(250))
    kind: Mapped[str] = mapped_column(String(40), index=True)
    external_reference: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Principal(Base):
    __tablename__ = "platform_principals"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    principal_type: Mapped[str] = mapped_column(String(24), default="USER", index=True)
    display_name: Mapped[str] = mapped_column(String(250))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ExternalIdentity(Base):
    __tablename__ = "platform_external_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_external_identity_issuer_subject"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_principals.id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(255))
    identity_type: Mapped[str] = mapped_column(String(32), default="OIDC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Base):
    __tablename__ = "platform_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "principal_id", name="uq_membership_tenant_principal"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_tenants.id", ondelete="CASCADE"), index=True
    )
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_principals.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_organizations.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    carrier_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("carriers.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Role(Base):
    __tablename__ = "platform_roles"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_role_tenant_code"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_tenants.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(String(500))
    system: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Permission(Base):
    __tablename__ = "platform_permissions"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RolePermission(Base):
    __tablename__ = "platform_role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_roles.id", ondelete="CASCADE"), index=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_permissions.id", ondelete="CASCADE"), index=True
    )


class MembershipRole(Base):
    __tablename__ = "platform_membership_roles"
    __table_args__ = (
        UniqueConstraint("membership_id", "role_id", name="uq_membership_role"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    membership_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_memberships.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_roles.id", ondelete="CASCADE"), index=True
    )
