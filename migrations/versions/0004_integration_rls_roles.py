"""integration RLS, bounded cross-tenant worker access and database roles

Revision ID: 0004_integration_rls_roles
Revises: 0003_integrations_durability
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_integration_rls_roles"
down_revision = "0003_integrations_durability"
branch_labels = None
depends_on = None

INTEGRATION_TABLES = (
    "integration_connections",
    "integration_webhook_keys",
    "integration_deliveries",
    "integration_delivery_attempts",
    "integration_inbox_messages",
    "integration_command_requests",
    "integration_provenance_entries",
)

INGRESS_TABLES = (
    "integration_connections",
    "integration_webhook_keys",
    "integration_inbox_messages",
    "integration_provenance_entries",
)

WORKER_CORE_TABLES = (
    "outbox_messages",
    "capabilities",
    "loads",
    "tracking_events",
    "operational_exceptions",
)

API_DML_TABLES = (
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
    "platform_tenants",
    "platform_organizations",
    "platform_principals",
    "platform_external_identities",
    "platform_roles",
    "platform_permissions",
    "platform_role_permissions",
    "platform_memberships",
    "platform_membership_roles",
    *INTEGRATION_TABLES,
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f'"{value}"' for value in values)


def _tenant_expression() -> str:
    return "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _execute(sql: str) -> None:
    """Execute exactly one top-level PostgreSQL statement.

    asyncpg rejects prepared statements containing multiple top-level SQL
    statements, even when PostgreSQL itself would accept the combined text.
    """

    op.execute(sa.text(sql))


def _replace_policy(
    *,
    table_name: str,
    policy_name: str,
    role_name: str,
    using: str,
    with_check: str,
) -> None:
    _execute(f'DROP POLICY IF EXISTS "{policy_name}" ON "{table_name}"')
    _execute(
        f'''CREATE POLICY "{policy_name}" ON "{table_name}"
            TO "{role_name}"
            USING ({using})
            WITH CHECK ({with_check})'''
    )


def upgrade() -> None:
    # These are NOLOGIN group roles. Deployment creates separate login users and
    # grants exactly one group role to each credential; no password is stored here.
    _execute(
        r'''
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'freight_api') THEN
        CREATE ROLE freight_api NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'freight_ingress') THEN
        CREATE ROLE freight_ingress NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'freight_worker') THEN
        CREATE ROLE freight_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
    END IF;
END;
$roles$
'''
    )

    _execute("GRANT USAGE ON SCHEMA public TO freight_api, freight_ingress, freight_worker")
    _execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_quoted(API_DML_TABLES)} TO freight_api"
    )
    _execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO freight_api")

    # A non-RLS route registry is intentionally narrow: it stores only an opaque
    # slug and UUIDs. Ingress can call the SECURITY DEFINER resolver but cannot
    # query this table directly or enumerate tenant routes.
    op.create_table(
        "integration_webhook_routes",
        sa.Column("webhook_slug", sa.String(120), primary_key=True),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("integration_connections.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
    )
    op.create_index(
        "ix_integration_webhook_routes_enabled",
        "integration_webhook_routes",
        ["enabled", "webhook_slug"],
    )
    _execute("REVOKE ALL ON TABLE integration_webhook_routes FROM PUBLIC")

    _execute(
        r'''
CREATE OR REPLACE FUNCTION public.freight_sync_webhook_route()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM public.integration_webhook_routes
        WHERE connection_id = OLD.id;
        RETURN OLD;
    END IF;

    INSERT INTO public.integration_webhook_routes (
        webhook_slug, connection_id, tenant_id, enabled, updated_at
    ) VALUES (
        NEW.webhook_slug, NEW.id, NEW.tenant_id, NEW.enabled, timezone('utc', now())
    )
    ON CONFLICT (connection_id) DO UPDATE SET
        webhook_slug = EXCLUDED.webhook_slug,
        tenant_id = EXCLUDED.tenant_id,
        enabled = EXCLUDED.enabled,
        updated_at = timezone('utc', now());
    RETURN NEW;
END;
$function$
'''
    )
    _execute("REVOKE ALL ON FUNCTION public.freight_sync_webhook_route() FROM PUBLIC")
    _execute(
        "DROP TRIGGER IF EXISTS trg_integration_connection_webhook_route "
        "ON public.integration_connections"
    )
    _execute(
        "CREATE TRIGGER trg_integration_connection_webhook_route "
        "AFTER INSERT OR UPDATE OR DELETE ON public.integration_connections "
        "FOR EACH ROW EXECUTE FUNCTION public.freight_sync_webhook_route()"
    )
    _execute(
        r'''
INSERT INTO public.integration_webhook_routes (
    webhook_slug, connection_id, tenant_id, enabled, updated_at
)
SELECT webhook_slug, id, tenant_id, enabled, timezone('utc', now())
FROM public.integration_connections
ON CONFLICT (connection_id) DO UPDATE SET
    webhook_slug = EXCLUDED.webhook_slug,
    tenant_id = EXCLUDED.tenant_id,
    enabled = EXCLUDED.enabled,
    updated_at = timezone('utc', now())
'''
    )
    _execute(
        r'''
CREATE OR REPLACE FUNCTION public.freight_resolve_webhook_connection(p_webhook_slug text)
RETURNS TABLE (connection_id uuid, tenant_id uuid)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $function$
    SELECT route.connection_id, route.tenant_id
    FROM public.integration_webhook_routes AS route
    WHERE route.webhook_slug = p_webhook_slug
      AND route.enabled
    LIMIT 1
$function$
'''
    )
    _execute(
        "REVOKE ALL ON FUNCTION public.freight_resolve_webhook_connection(text) FROM PUBLIC"
    )
    _execute(
        "GRANT EXECUTE ON FUNCTION public.freight_resolve_webhook_connection(text) "
        "TO freight_ingress"
    )

    tenant_expression = _tenant_expression()
    for table_name in INTEGRATION_TABLES:
        _execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        _execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        _replace_policy(
            table_name=table_name,
            policy_name=f"{table_name}_api_tenant",
            role_name="freight_api",
            using=tenant_expression,
            with_check=tenant_expression,
        )
        _replace_policy(
            table_name=table_name,
            policy_name=f"{table_name}_worker_bounded",
            role_name="freight_worker",
            using="true",
            with_check="true",
        )

    for table_name in INGRESS_TABLES:
        _replace_policy(
            table_name=table_name,
            policy_name=f"{table_name}_ingress_tenant",
            role_name="freight_ingress",
            using=tenant_expression,
            with_check=tenant_expression,
        )

    _execute(
        "GRANT SELECT ON TABLE integration_connections, integration_webhook_keys, "
        "integration_inbox_messages, integration_provenance_entries TO freight_ingress"
    )
    _execute(
        "GRANT INSERT ON TABLE integration_inbox_messages, "
        "integration_provenance_entries TO freight_ingress"
    )

    _execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE {_quoted(INTEGRATION_TABLES)} TO freight_worker"
    )
    _execute("GRANT SELECT, UPDATE ON TABLE outbox_messages TO freight_worker")
    _execute("GRANT SELECT ON TABLE capabilities, loads TO freight_worker")
    _execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE tracking_events, "
        "operational_exceptions TO freight_worker"
    )

    # The worker is not BYPASSRLS. Cross-tenant access exists only on the exact
    # queue/domain tables required for bounded claims and translations.
    for table_name in WORKER_CORE_TABLES:
        _execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        _execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        _replace_policy(
            table_name=table_name,
            policy_name=f"{table_name}_worker_bounded",
            role_name="freight_worker",
            using="true",
            with_check="true",
        )


def downgrade() -> None:
    for table_name in WORKER_CORE_TABLES:
        _execute(f'DROP POLICY IF EXISTS "{table_name}_worker_bounded" ON "{table_name}"')

    for table_name in INTEGRATION_TABLES:
        for suffix in ("api_tenant", "worker_bounded", "ingress_tenant"):
            _execute(f'DROP POLICY IF EXISTS "{table_name}_{suffix}" ON "{table_name}"')

    _execute(
        "DROP TRIGGER IF EXISTS trg_integration_connection_webhook_route "
        "ON public.integration_connections"
    )
    _execute("DROP FUNCTION IF EXISTS public.freight_resolve_webhook_connection(text)")
    _execute("DROP FUNCTION IF EXISTS public.freight_sync_webhook_route()")
    op.drop_index(
        "ix_integration_webhook_routes_enabled",
        table_name="integration_webhook_routes",
    )
    op.drop_table("integration_webhook_routes")

    # Group roles are intentionally retained on downgrade because external login
    # roles may already be members. A DBA can remove unused roles explicitly.
