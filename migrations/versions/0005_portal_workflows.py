"""durable customer/carrier portal bindings, submissions and access evidence

Revision ID: 0005_portal_workflows
Revises: 0004_integration_rls_roles
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_portal_workflows"
down_revision = "0004_integration_rls_roles"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text("timezone('utc', now())")

PORTAL_TABLES = (
    "portal_principal_bindings",
    "portal_claim_submissions",
    "portal_access_audit",
    "portal_tracking_submissions",
    "portal_carrier_evidence_submissions",
)


def _tenant_policy(table_name: str) -> None:
    expression = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    policy_name = f"{table_name}_api_tenant"
    op.execute(sa.text(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'DROP POLICY IF EXISTS "{policy_name}" ON "{table_name}"'))
    op.execute(
        sa.text(
            f'''CREATE POLICY "{policy_name}" ON "{table_name}"
                TO freight_api
                USING ({expression})
                WITH CHECK ({expression})'''
        )
    )


def upgrade() -> None:
    op.create_table(
        "portal_principal_bindings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("principal_issuer", sa.String(400), nullable=False),
        sa.Column("principal_subject", sa.String(220), nullable=False),
        sa.Column("portal_kind", sa.String(20), nullable=False),
        sa.Column("resource_id", UUID, nullable=False),
        sa.Column("display_label", sa.String(220), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("metadata_json", JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by", sa.String(220), nullable=False),
        sa.Column("revoked_by", sa.String(220)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "portal_kind IN ('CUSTOMER', 'CARRIER')",
            name="ck_portal_binding_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
            name="ck_portal_binding_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_issuer",
            "principal_subject",
            "portal_kind",
            name="uq_portal_principal_kind",
        ),
    )
    op.create_index(
        "ix_portal_binding_lookup",
        "portal_principal_bindings",
        ["tenant_id", "principal_issuer", "principal_subject", "portal_kind", "status"],
    )
    op.create_index(
        "ix_portal_binding_resource",
        "portal_principal_bindings",
        ["tenant_id", "portal_kind", "resource_id", "status"],
    )
    op.create_index(
        "ix_portal_principal_bindings_tenant_id",
        "portal_principal_bindings",
        ["tenant_id"],
    )

    op.create_table(
        "portal_claim_submissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column(
            "customer_id",
            UUID,
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "shipment_id",
            UUID,
            sa.ForeignKey("shipments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("submitted_by_subject", sa.String(220), nullable=False),
        sa.Column("submission_key", sa.String(120), nullable=False),
        sa.Column("claim_type", sa.String(80), nullable=False),
        sa.Column("title", sa.String(220), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("claimed_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("evidence_document_ids", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(30), nullable=False, server_default="SUBMITTED"),
        sa.Column("internal_claim_id", UUID),
        sa.Column("customer_visible_note", sa.Text()),
        sa.Column("internal_note", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('SUBMITTED', 'UNDER_REVIEW', 'NEEDS_INFORMATION', 'ACCEPTED', 'DENIED', 'WITHDRAWN')",
            name="ck_portal_claim_status",
        ),
        sa.CheckConstraint("claimed_amount >= 0", name="ck_portal_claim_amount"),
        sa.UniqueConstraint(
            "tenant_id",
            "customer_id",
            "submission_key",
            name="uq_portal_claim_submission_key",
        ),
    )
    op.create_index(
        "ix_portal_claim_customer",
        "portal_claim_submissions",
        ["tenant_id", "customer_id", "status", "created_at"],
    )
    op.create_index(
        "ix_portal_claim_shipment",
        "portal_claim_submissions",
        ["tenant_id", "shipment_id", "created_at"],
    )
    op.create_index(
        "ix_portal_claim_submissions_tenant_id",
        "portal_claim_submissions",
        ["tenant_id"],
    )

    op.create_table(
        "portal_access_audit",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("principal_issuer", sa.String(400), nullable=False),
        sa.Column("principal_subject", sa.String(220), nullable=False),
        sa.Column("portal_kind", sa.String(20), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("resource_type", sa.String(120), nullable=False),
        sa.Column("resource_id", UUID),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("correlation_id", sa.String(120), nullable=False),
        sa.Column("metadata_json", JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index(
        "ix_portal_access_principal",
        "portal_access_audit",
        ["tenant_id", "principal_subject", "portal_kind", "occurred_at"],
    )
    op.create_index(
        "ix_portal_access_resource",
        "portal_access_audit",
        ["tenant_id", "resource_type", "resource_id", "occurred_at"],
    )
    op.create_index(
        "ix_portal_access_audit_tenant_id",
        "portal_access_audit",
        ["tenant_id"],
    )

    op.create_table(
        "portal_tracking_submissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column(
            "carrier_id",
            UUID,
            sa.ForeignKey("carriers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "load_id",
            UUID,
            sa.ForeignKey("loads.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.String(220), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("tracking_event_id", UUID),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACCEPTED"),
        sa.Column("error_code", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'PROCESSED', 'REJECTED')",
            name="ck_portal_tracking_status",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_portal_tracking_payload_hash",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "carrier_id",
            "source_event_id",
            name="uq_portal_tracking_source_event",
        ),
    )
    op.create_index(
        "ix_portal_tracking_load",
        "portal_tracking_submissions",
        ["tenant_id", "load_id", "occurred_at"],
    )
    op.create_index(
        "ix_portal_tracking_submissions_tenant_id",
        "portal_tracking_submissions",
        ["tenant_id"],
    )

    op.create_table(
        "portal_carrier_evidence_submissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column(
            "carrier_id",
            UUID,
            sa.ForeignKey("carriers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("submitted_by_subject", sa.String(220), nullable=False),
        sa.Column("submission_key", sa.String(120), nullable=False),
        sa.Column("evidence_type", sa.String(40), nullable=False),
        sa.Column("identifier_hash", sa.String(64)),
        sa.Column("evidence_document_ids", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metadata_json", JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(30), nullable=False, server_default="SUBMITTED"),
        sa.Column("reviewer_note", sa.Text()),
        sa.Column("reviewed_by", sa.String(220)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("authoritative_record_id", UUID),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "evidence_type IN ('AUTHORITY', 'AUTO_LIABILITY', 'CARGO', 'GENERAL_LIABILITY', 'WORKERS_COMP', 'SAFETY')",
            name="ck_portal_evidence_type",
        ),
        sa.CheckConstraint(
            "status IN ('SUBMITTED', 'UNDER_REVIEW', 'ACCEPTED', 'REJECTED', 'SUPERSEDED')",
            name="ck_portal_evidence_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "carrier_id",
            "submission_key",
            name="uq_portal_carrier_evidence_submission_key",
        ),
    )
    op.create_index(
        "ix_portal_evidence_carrier",
        "portal_carrier_evidence_submissions",
        ["tenant_id", "carrier_id", "status", "created_at"],
    )
    op.create_index(
        "ix_portal_carrier_evidence_submissions_tenant_id",
        "portal_carrier_evidence_submissions",
        ["tenant_id"],
    )

    for table_name in PORTAL_TABLES:
        _tenant_policy(table_name)

    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
            + ", ".join(f'\"{name}\"' for name in PORTAL_TABLES)
            + " TO freight_api"
        )
    )


def downgrade() -> None:
    for table_name in reversed(PORTAL_TABLES):
        op.execute(
            sa.text(
                f'DROP POLICY IF EXISTS "{table_name}_api_tenant" ON "{table_name}"'
            )
        )

    op.drop_table("portal_carrier_evidence_submissions")
    op.drop_table("portal_tracking_submissions")
    op.drop_table("portal_access_audit")
    op.drop_table("portal_claim_submissions")
    op.drop_table("portal_principal_bindings")
