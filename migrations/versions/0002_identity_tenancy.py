"""persistent identity, tenant membership, RBAC and row policies

Revision ID: 0002_identity_tenancy
Revises: 0001_foundation
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_identity_tenancy"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)

TENANT_TABLES = (
    "capabilities",
    "customers",
    "customer_locations",
    "customer_contacts",
    "carriers",
    "carrier_contacts",
    "carrier_equipment",
    "carrier_compliance",
    "carrier_insurance",
    "quotes",
    "quote_versions",
    "shipments",
    "shipment_legs",
    "stops",
    "loads",
    "load_shipment_legs",
    "tenders",
    "tracking_events",
    "documents",
    "invoices",
    "carrier_settlements",
    "claims",
    "operational_exceptions",
    "audit_entries",
    "idempotency_records",
    "outbox_messages",
    "inbox_messages",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "platform_tenants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(250), nullable=False),
        sa.Column("status", sa.String(32), server_default="ACTIVE", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("slug", name="uq_platform_tenant_slug"),
    )
    op.create_index("ix_platform_tenants_slug", "platform_tenants", ["slug"], unique=True)
    op.create_index("ix_platform_tenants_status", "platform_tenants", ["status"])

    op.create_table(
        "platform_organizations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("platform_tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(250), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("external_reference", sa.String(160)),
        sa.Column("status", sa.String(32), server_default="ACTIVE", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "name", "kind", name="uq_platform_organization_name"
        ),
    )
    op.create_index(
        "ix_platform_organizations_tenant_id", "platform_organizations", ["tenant_id"]
    )
    op.create_index("ix_platform_organizations_kind", "platform_organizations", ["kind"])
    op.create_index(
        "ix_platform_organizations_status", "platform_organizations", ["status"]
    )

    op.create_table(
        "platform_principals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("principal_type", sa.String(24), server_default="USER", nullable=False),
        sa.Column("display_name", sa.String(250), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("status", sa.String(32), server_default="ACTIVE", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_platform_principals_email", "platform_principals", ["email"])
    op.create_index("ix_platform_principals_status", "platform_principals", ["status"])
    op.create_index(
        "ix_platform_principals_type", "platform_principals", ["principal_type"]
    )

    op.create_table(
        "platform_external_identities",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "principal_id",
            UUID,
            sa.ForeignKey("platform_principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("identity_type", sa.String(32), server_default="OIDC", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "issuer", "subject", name="uq_external_identity_issuer_subject"
        ),
    )
    op.create_index(
        "ix_platform_external_identities_principal_id",
        "platform_external_identities",
        ["principal_id"],
    )

    op.create_table(
        "platform_roles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("platform_tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "code", name="uq_role_tenant_code"),
    )
    op.create_index("ix_platform_roles_tenant_id", "platform_roles", ["tenant_id"])

    op.create_table(
        "platform_permissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("code", sa.String(140), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_platform_permission_code"),
    )
    op.create_index(
        "ix_platform_permissions_code", "platform_permissions", ["code"], unique=True
    )

    op.create_table(
        "platform_memberships",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("platform_tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            UUID,
            sa.ForeignKey("platform_principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("platform_organizations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "customer_id", UUID, sa.ForeignKey("customers.id", ondelete="SET NULL")
        ),
        sa.Column(
            "carrier_id", UUID, sa.ForeignKey("carriers.id", ondelete="SET NULL")
        ),
        sa.Column("status", sa.String(32), server_default="ACTIVE", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", name="uq_membership_tenant_principal"
        ),
    )
    for column in (
        "tenant_id",
        "principal_id",
        "organization_id",
        "customer_id",
        "carrier_id",
        "status",
    ):
        op.create_index(
            f"ix_platform_memberships_{column}", "platform_memberships", [column]
        )

    op.create_table(
        "platform_role_permissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "role_id",
            UUID,
            sa.ForeignKey("platform_roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            UUID,
            sa.ForeignKey("platform_permissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )
    op.create_index(
        "ix_platform_role_permissions_role_id", "platform_role_permissions", ["role_id"]
    )
    op.create_index(
        "ix_platform_role_permissions_permission_id",
        "platform_role_permissions",
        ["permission_id"],
    )

    op.create_table(
        "platform_membership_roles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "membership_id",
            UUID,
            sa.ForeignKey("platform_memberships.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            UUID,
            sa.ForeignKey("platform_roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("membership_id", "role_id", name="uq_membership_role"),
    )
    op.create_index(
        "ix_platform_membership_roles_membership_id",
        "platform_membership_roles",
        ["membership_id"],
    )
    op.create_index(
        "ix_platform_membership_roles_role_id",
        "platform_membership_roles",
        ["role_id"],
    )

    # Production API credentials must use a non-owner database role because
    # PostgreSQL table owners normally bypass RLS. Worker access is separate.
    for table in TENANT_TABLES:
        policy = f"tenant_isolation_{table}"
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'''CREATE POLICY "{policy}" ON "{table}"
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)'''
        )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        policy = f"tenant_isolation_{table}"
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    op.drop_table("platform_membership_roles")
    op.drop_table("platform_role_permissions")
    op.drop_table("platform_memberships")
    op.drop_table("platform_permissions")
    op.drop_table("platform_roles")
    op.drop_table("platform_external_identities")
    op.drop_table("platform_principals")
    op.drop_table("platform_organizations")
    op.drop_table("platform_tenants")
