from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()


def _build_engine(database_url: str, *, pool_size: int, max_overflow: int) -> AsyncEngine:
    if settings.environment.lower() == "test":
        # TestClient and pytest-asyncio may execute requests on different event loops.
        # asyncpg connections are loop-bound, so never retain them between test loops.
        return create_async_engine(database_url, poolclass=NullPool)
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1_800,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


engine = _build_engine(settings.database_url, pool_size=10, max_overflow=20)
ingress_engine = _build_engine(
    settings.resolved_ingress_database_url,
    pool_size=5,
    max_overflow=10,
)
worker_engine = _build_engine(
    settings.resolved_worker_database_url,
    pool_size=5,
    max_overflow=5,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
IngressSessionLocal = async_sessionmaker(
    ingress_engine,
    expire_on_commit=False,
    autoflush=False,
)
WorkerSessionLocal = async_sessionmaker(
    worker_engine,
    expire_on_commit=False,
    autoflush=False,
)


@event.listens_for(Session, "after_begin")
def _restore_request_context(session: Session, _transaction: object, connection: object) -> None:
    """Reapply transaction-local tenant context after a commit starts a new transaction."""

    tenant_id = session.info.get("tenant_id")
    actor_id = session.info.get("actor_id")
    if tenant_id:
        connection.execute(  # type: ignore[attr-defined]
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
    if actor_id:
        connection.execute(  # type: ignore[attr-defined]
            text("SELECT set_config('app.actor_id', :actor_id, true)"),
            {"actor_id": str(actor_id)},
        )


async def set_session_context(session: AsyncSession, tenant_id: UUID, actor_id: str) -> None:
    """Set the authoritative tenant/actor context used by PostgreSQL policies."""

    session.info["tenant_id"] = tenant_id
    session.info["actor_id"] = actor_id
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
    await session.execute(
        text("SELECT set_config('app.actor_id', :actor_id, true)"),
        {"actor_id": actor_id},
    )


async def _session_dependency(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in _session_dependency(SessionLocal):
        yield session


async def get_ingress_db() -> AsyncIterator[AsyncSession]:
    """Use the dedicated narrow credential for unauthenticated webhook ingress."""

    async for session in _session_dependency(IngressSessionLocal):
        yield session


async def get_worker_db() -> AsyncIterator[AsyncSession]:
    """Use the bounded cross-tenant worker credential outside request handling."""

    async for session in _session_dependency(WorkerSessionLocal):
        yield session
