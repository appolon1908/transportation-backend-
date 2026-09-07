"""Forward-port tenant isolation for persistent identity and RBAC.

Revision ID: 0006_identity_rbac_rls
Revises: 0005_portal_workflows

Do not introduce the historical unmerged 0002b sibling into the accepted chain.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_identity_rbac_rls"
down_revision = "0005_portal_workflows"
branch_labels = None
depends_on = None

DIRECT_TABLES = ("platform_organizations", "platform_roles", "platform_memberships")
TABLES = (*DIRECT_TABLES, "platform_role_permissions", "platform_membership_roles")
TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def expressions() -> dict[str, str]:
    result = {table: f"tenant_id = {TENANT}" for table in DIRECT_TABLES}
    result["platform_role_permissions"] = (
        "EXISTS (SELECT 1 FROM platform_roles role_scope "
        f"WHERE role_scope.id = role_id AND role_scope.tenant_id = {TENANT})"
    )
    result["platform_membership_roles"] = (
        "EXISTS (SELECT 1 FROM platform_memberships membership_scope "
        "JOIN platform_roles role_scope "
        "ON role_scope.tenant_id = membership_scope.tenant_id "
        "WHERE membership_scope.id = membership_id AND role_scope.id = role_id "
        f"AND membership_scope.tenant_id = {TENANT})"
    )
    return result


def upgrade() -> None:
    for table, predicate in expressions().items():
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        # A permissive owner policy supplies normal access. The matching
        # restrictive boundary cannot be weakened by another permissive policy.
        for mode, suffix in (("PERMISSIVE", "access"), ("RESTRICTIVE", "boundary")):
            op.execute(sa.text(
                f'CREATE POLICY "identity_tenant_{suffix}_{table}" ON "{table}" '
                f'AS {mode} USING ({predicate}) WITH CHECK ({predicate})'
            ))


def downgrade() -> None:
    for table in reversed(TABLES):
        for suffix in ("boundary", "access"):
            op.execute(sa.text(f'DROP POLICY "identity_tenant_{suffix}_{table}" ON "{table}"'))
        # Restore the documented 0005 state. Application rollback should normally
        # retain this additive database hardening rather than downgrade it live.
        op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
