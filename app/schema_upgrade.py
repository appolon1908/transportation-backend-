"""Explicit core/compliance upgrade order; never called by API startup.

Preserves the published compliance migration's exact core-0005 precondition.
No stamping, implicit downgrade, or fabricated version readback is permitted.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.release import CANONICAL_MIGRATION_HEAD

COMPATIBLE_CORE = "0005_portal_workflows"
COMPLIANCE_HEAD = "0001_carrier_readiness"
KNOWN_CORE = (
    "0001_foundation", "0002_identity_tenancy", "0003_integrations_durability",
    "0004_integration_rls_roles", COMPATIBLE_CORE, "0006_identity_rbac_rls", "0007_document_storage", "0008_document_verification_lease",
)


def upgrade_plan(core: tuple[str, ...], compliance: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if len(core) > 1 or len(compliance) > 1:
        raise ValueError("schema_upgrade_refuses_multiple_heads")
    current = core[0] if core else None
    if current is not None and current not in KNOWN_CORE:
        raise ValueError("schema_upgrade_refuses_unknown_core")
    if compliance not in ((), (COMPLIANCE_HEAD,)):
        raise ValueError("schema_upgrade_refuses_unknown_compliance")
    if compliance:
        if current not in (COMPATIBLE_CORE, "0006_identity_rbac_rls", "0007_document_storage", CANONICAL_MIGRATION_HEAD):
            raise ValueError("schema_upgrade_refuses_inconsistent_history")
        return (("alembic.ini", "head"), ("alembic-compliance.ini", "head"))
    if current in ("0006_identity_rbac_rls", "0007_document_storage", CANONICAL_MIGRATION_HEAD):
        raise ValueError("compliance_missing_after_identity_upgrade_requires_review")
    return (
        ("alembic.ini", COMPATIBLE_CORE),
        ("alembic-compliance.ini", "head"),
        ("alembic.ini", "head"),
    )


def read_heads(connection) -> tuple[tuple[str, ...], tuple[str, ...]]:
    core = MigrationContext.configure(connection).get_current_heads()
    compliance = MigrationContext.configure(connection, opts={
        "version_table": "alembic_version_compliance",
    }).get_current_heads()
    return tuple(core), tuple(compliance)


async def upgrade() -> None:
    root = Path.cwd()
    if ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads() != [CANONICAL_MIGRATION_HEAD]:
        raise ValueError("source_migration_head_mismatch")
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            locked = await connection.scalar(text("SELECT pg_try_advisory_lock(72820431)"))
            if not locked:
                raise RuntimeError("another_schema_upgrade_is_running")
            try:
                plan = upgrade_plan(*(await connection.run_sync(read_heads)))
                # Release read locks while keeping the session advisory lock.
                await connection.commit()
                for config, target in plan:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "alembic", "-c", config, "upgrade", target,
                        cwd=root,
                    )
                    try:
                        status = await process.wait()
                    except BaseException:
                        if process.returncode is None:
                            process.terminate()
                        await process.wait()
                        raise
                    if status != 0:
                        raise RuntimeError("schema_upgrade_step_failed")
                final = await connection.run_sync(read_heads)
                if final != ((CANONICAL_MIGRATION_HEAD,), (COMPLIANCE_HEAD,)):
                    raise RuntimeError("schema_upgrade_readback_failed")
                print("CORE_AND_COMPLIANCE_SCHEMA=VERIFIED")
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(72820431)"))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(upgrade())
