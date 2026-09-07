"""Durable inbound/outbound integration subsystem.

Provider credentials are represented only by secret references. The runtime
resolves those references at delivery or verification time and never persists
secret material in PostgreSQL.
"""

from app.integrations.api import router

__all__ = ["router"]
