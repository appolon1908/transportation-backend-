"""Durable version-bound document objects, after accepted identity/RBAC isolation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_document_storage"
down_revision = "0006_identity_rbac_rls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("document_objects",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(63), nullable=False),
        sa.Column("version_id", sa.String(1024)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_code", sa.String(80)),
        sa.CheckConstraint("size_bytes BETWEEN 1 AND 10485760", name="ck_document_object_size"),
        sa.CheckConstraint("retain_until > expires_at", name="ck_document_retention"),
    )
    op.create_index("ix_document_objects_tenant_id", "document_objects", ["tenant_id"])
    op.execute("ALTER TABLE document_objects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_objects FORCE ROW LEVEL SECURITY")
    predicate = """tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id
                    AND d.tenant_id = document_objects.tenant_id)"""
    for mode, suffix in (("PERMISSIVE", "access"), ("RESTRICTIVE", "boundary")):
        op.execute(f"CREATE POLICY document_tenant_{suffix} ON document_objects AS {mode} USING ({predicate}) WITH CHECK ({predicate})")
    # No ingress or cross-tenant worker grant. Scanning is an authenticated command.
    op.execute("GRANT SELECT, INSERT, UPDATE ON document_objects TO freight_api")


def downgrade():
    op.drop_table("document_objects")
