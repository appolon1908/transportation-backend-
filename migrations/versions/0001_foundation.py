"""initial freight platform foundation

Revision ID: 0001_foundation
Revises:
Create Date: 2026-08-23
"""

from alembic import op
from sqlalchemy import MetaData

from app.db import Base
import app.models  # noqa: F401

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None

# Alembic revisions must never call create_all against today's complete metadata.
# This explicit historical table set prevents later identity, integration,
# compliance and portal models from being created by revision 0001.
CORE_TABLE_NAMES = (
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


def _foundation_metadata() -> MetaData:
    metadata = MetaData()
    missing = [name for name in CORE_TABLE_NAMES if name not in Base.metadata.tables]
    if missing:
        raise RuntimeError(f"Foundation metadata is missing historical tables: {', '.join(missing)}")
    for table_name in CORE_TABLE_NAMES:
        Base.metadata.tables[table_name].to_metadata(metadata)
    return metadata


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    _foundation_metadata().create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    # Leave the shared PostGIS extension installed because other schemas may use it.
    _foundation_metadata().drop_all(bind=op.get_bind(), checkfirst=True)
