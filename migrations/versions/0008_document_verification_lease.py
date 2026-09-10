"""Short-lived document verification claims without holding provider-time locks."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_document_verification_lease"
down_revision = "0007_document_storage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_objects", sa.Column("verification_token", postgresql.UUID(as_uuid=True)))
    op.add_column("document_objects", sa.Column("verification_expires_at", sa.DateTime(timezone=True)))


def downgrade():
    op.execute("UPDATE documents SET status='PENDING_UPLOAD' WHERE status='VERIFYING'")
    op.drop_column("document_objects", "verification_expires_at")
    op.drop_column("document_objects", "verification_token")
