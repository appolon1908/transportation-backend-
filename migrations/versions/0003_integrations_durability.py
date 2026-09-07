"""durable integration connections, inbox, deliveries and provenance

Revision ID: 0003_integrations_durability
Revises: 0002_identity_tenancy
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_integrations_durability"
down_revision = "0002_identity_tenancy"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_JSON = postgresql.JSONB(astext_type=sa.Text())
_NOW = sa.text("timezone('utc', now())")


def upgrade() -> None:
    op.create_table(
        "integration_connections",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("webhook_slug", sa.String(120), nullable=False),
        sa.Column("base_url", sa.String(1000), nullable=False),
        sa.Column("endpoint_path", sa.String(500)),
        sa.Column("secret_ref", sa.String(300)),
        sa.Column("signing_secret_ref", sa.String(300)),
        sa.Column("signing_key_id", sa.String(120)),
        sa.Column("capability_code", sa.String(160)),
        sa.Column("event_types", _JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("configuration", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.CheckConstraint("kind IN ('ODOO_JSON2', 'N8N_WEBHOOK', 'SIGNED_WEBHOOK')", name="ck_integration_connection_kind"),
        sa.CheckConstraint("timeout_seconds BETWEEN 1 AND 120", name="ck_integration_connection_timeout"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 25", name="ck_integration_connection_attempts"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_integration_connection_tenant_name"),
        sa.UniqueConstraint("webhook_slug", name="uq_integration_connection_webhook_slug"),
    )
    op.create_index("ix_integration_connections_tenant_id", "integration_connections", ["tenant_id"])
    op.create_index("ix_integration_connections_tenant_kind", "integration_connections", ["tenant_id", "kind"])

    op.create_table(
        "integration_webhook_keys",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, sa.ForeignKey("integration_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_id", sa.String(120), nullable=False),
        sa.Column("secret_ref", sa.String(300), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expires_at IS NULL OR not_before IS NULL OR expires_at > not_before", name="ck_integration_webhook_key_window"),
        sa.UniqueConstraint("connection_id", "key_id", name="uq_integration_webhook_key"),
    )
    op.create_index("ix_integration_webhook_keys_tenant_id", "integration_webhook_keys", ["tenant_id"])
    op.create_index("ix_integration_webhook_keys_connection_id", "integration_webhook_keys", ["connection_id"])
    op.create_index("ix_integration_webhook_keys_active", "integration_webhook_keys", ["connection_id", "active"])

    op.create_table(
        "integration_deliveries",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, sa.ForeignKey("integration_connections.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("outbox_id", _UUID, sa.ForeignKey("outbox_messages.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("event_type", sa.String(180), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("claim_token", _UUID),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_status_code", sa.Integer()),
        sa.Column("last_error_code", sa.String(120)),
        sa.Column("last_error_detail", sa.Text()),
        sa.Column("last_response_hash", sa.String(64)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.CheckConstraint("attempts >= 0", name="ck_integration_delivery_attempt_count"),
        sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="ck_integration_delivery_payload_hash"),
        sa.UniqueConstraint("connection_id", "outbox_id", name="uq_integration_delivery_outbox"),
    )
    op.create_index("ix_integration_deliveries_tenant_id", "integration_deliveries", ["tenant_id"])
    op.create_index("ix_integration_deliveries_connection_id", "integration_deliveries", ["connection_id"])
    op.create_index("ix_integration_deliveries_outbox_id", "integration_deliveries", ["outbox_id"])
    op.create_index("ix_integration_deliveries_event_id", "integration_deliveries", ["event_id"])
    op.create_index("ix_integration_deliveries_event_type", "integration_deliveries", ["event_type"])
    op.create_index("ix_integration_deliveries_claim", "integration_deliveries", ["status", "next_attempt_at", "claim_expires_at"])
    op.create_index("ix_integration_deliveries_tenant_created", "integration_deliveries", ["tenant_id", "created_at"])

    op.create_table(
        "integration_delivery_attempts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("delivery_id", _UUID, sa.ForeignKey("integration_deliveries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_hash", sa.String(64)),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(120)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.CheckConstraint("attempt_number > 0", name="ck_integration_delivery_attempt_number"),
        sa.UniqueConstraint("delivery_id", "attempt_number", name="uq_integration_delivery_attempt"),
    )
    op.create_index("ix_integration_delivery_attempts_tenant_id", "integration_delivery_attempts", ["tenant_id"])
    op.create_index("ix_integration_delivery_attempts_delivery_id", "integration_delivery_attempts", ["delivery_id"])

    op.create_table(
        "integration_inbox_messages",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, sa.ForeignKey("integration_connections.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("external_event_id", sa.String(240), nullable=False),
        sa.Column("event_type", sa.String(180), nullable=False),
        sa.Column("key_id", sa.String(120), nullable=False),
        sa.Column("signature_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("signature_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("headers_redacted", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(40), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("claim_token", _UUID),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("translated_type", sa.String(180)),
        sa.Column("translated_resource_id", _UUID),
        sa.Column("last_error_code", sa.String(120)),
        sa.Column("last_error_detail", sa.Text()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.CheckConstraint("raw_hash ~ '^[0-9a-f]{64}$'", name="ck_integration_inbox_raw_hash"),
        sa.UniqueConstraint("connection_id", "external_event_id", name="uq_integration_inbox_external_event"),
    )
    op.create_index("ix_integration_inbox_tenant_id", "integration_inbox_messages", ["tenant_id"])
    op.create_index("ix_integration_inbox_connection_id", "integration_inbox_messages", ["connection_id"])
    op.create_index("ix_integration_inbox_event_type", "integration_inbox_messages", ["event_type"])
    op.create_index("ix_integration_inbox_claim", "integration_inbox_messages", ["status", "next_attempt_at", "claim_expires_at"])
    op.create_index("ix_integration_inbox_tenant_received", "integration_inbox_messages", ["tenant_id", "received_at"])

    op.create_table(
        "integration_command_requests",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("inbox_id", _UUID, sa.ForeignKey("integration_inbox_messages.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("command_type", sa.String(180), nullable=False),
        sa.Column("command_payload", _JSON, nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="RECEIVED"),
        sa.Column("result", _JSON),
        sa.Column("last_error_code", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("inbox_id", name="uq_integration_command_inbox"),
    )
    op.create_index("ix_integration_command_tenant_id", "integration_command_requests", ["tenant_id"])
    op.create_index("ix_integration_command_status", "integration_command_requests", ["tenant_id", "status", "created_at"])

    op.create_table(
        "integration_provenance_entries",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("chain_scope", sa.String(180), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_hash", sa.String(64)),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(180), nullable=False),
        sa.Column("entity_type", sa.String(80), nullable=False),
        sa.Column("entity_id", _UUID, nullable=False),
        sa.Column("correlation_id", sa.String(120)),
        sa.Column("metadata_json", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.CheckConstraint("sequence > 0", name="ck_integration_provenance_sequence"),
        sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="ck_integration_provenance_payload_hash"),
        sa.CheckConstraint("entry_hash ~ '^[0-9a-f]{64}$'", name="ck_integration_provenance_entry_hash"),
        sa.UniqueConstraint("tenant_id", "chain_scope", "sequence", name="uq_integration_provenance_sequence"),
        sa.UniqueConstraint("tenant_id", "entry_hash", name="uq_integration_provenance_hash"),
    )
    op.create_index("ix_integration_provenance_tenant_id", "integration_provenance_entries", ["tenant_id"])
    op.create_index("ix_integration_provenance_entity", "integration_provenance_entries", ["tenant_id", "entity_type", "entity_id"])

    # The integration ingress and worker require controlled cross-tenant discovery
    # before they can set transaction-local tenant context. These tables therefore
    # rely on explicit tenant predicates and dedicated least-privilege DB roles in
    # this revision. RLS activation is deliberately deferred until those production
    # roles and SECURITY DEFINER lookup functions are installed and reviewed.


def downgrade() -> None:
    op.drop_index("ix_integration_provenance_entity", table_name="integration_provenance_entries")
    op.drop_index("ix_integration_provenance_tenant_id", table_name="integration_provenance_entries")
    op.drop_table("integration_provenance_entries")
    op.drop_index("ix_integration_command_status", table_name="integration_command_requests")
    op.drop_index("ix_integration_command_tenant_id", table_name="integration_command_requests")
    op.drop_table("integration_command_requests")
    op.drop_index("ix_integration_inbox_tenant_received", table_name="integration_inbox_messages")
    op.drop_index("ix_integration_inbox_claim", table_name="integration_inbox_messages")
    op.drop_index("ix_integration_inbox_event_type", table_name="integration_inbox_messages")
    op.drop_index("ix_integration_inbox_connection_id", table_name="integration_inbox_messages")
    op.drop_index("ix_integration_inbox_tenant_id", table_name="integration_inbox_messages")
    op.drop_table("integration_inbox_messages")
    op.drop_index("ix_integration_delivery_attempts_delivery_id", table_name="integration_delivery_attempts")
    op.drop_index("ix_integration_delivery_attempts_tenant_id", table_name="integration_delivery_attempts")
    op.drop_table("integration_delivery_attempts")
    op.drop_index("ix_integration_deliveries_tenant_created", table_name="integration_deliveries")
    op.drop_index("ix_integration_deliveries_claim", table_name="integration_deliveries")
    op.drop_index("ix_integration_deliveries_event_type", table_name="integration_deliveries")
    op.drop_index("ix_integration_deliveries_event_id", table_name="integration_deliveries")
    op.drop_index("ix_integration_deliveries_outbox_id", table_name="integration_deliveries")
    op.drop_index("ix_integration_deliveries_connection_id", table_name="integration_deliveries")
    op.drop_index("ix_integration_deliveries_tenant_id", table_name="integration_deliveries")
    op.drop_table("integration_deliveries")
    op.drop_index("ix_integration_webhook_keys_active", table_name="integration_webhook_keys")
    op.drop_index("ix_integration_webhook_keys_connection_id", table_name="integration_webhook_keys")
    op.drop_index("ix_integration_webhook_keys_tenant_id", table_name="integration_webhook_keys")
    op.drop_table("integration_webhook_keys")
    op.drop_index("ix_integration_connections_tenant_kind", table_name="integration_connections")
    op.drop_index("ix_integration_connections_tenant_id", table_name="integration_connections")
    op.drop_table("integration_connections")
