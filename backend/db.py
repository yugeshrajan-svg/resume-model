"""Database engines, tenant-scoped sessions, and the tenant-filtered repository.

Isolation is enforced twice:
  1. ``TenantRepo`` adds ``company_id = :tenant`` to every query and stamps it on every insert;
  2. ``tenant_session`` runs as the app role with ``app.company_id`` set for the transaction, so Postgres
     row-level security hides (and refuses to write) other tenants' rows even if a query forgets the filter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.config import PlatformSettings
from backend.models import Base

M = TypeVar("M", bound=Base)


def _engine(url: str, pool_size: int) -> AsyncEngine:
    if pool_size == 0:
        return create_async_engine(url, poolclass=NullPool)
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True)


@dataclass
class Database:
    app_engine: AsyncEngine
    system_engine: AsyncEngine
    _app_sessions: async_sessionmaker[AsyncSession] = field(init=False)
    _system_sessions: async_sessionmaker[AsyncSession] = field(init=False)

    def __post_init__(self) -> None:
        self._app_sessions = async_sessionmaker(self.app_engine, expire_on_commit=False)
        self._system_sessions = async_sessionmaker(self.system_engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: PlatformSettings) -> Database:
        return cls(
            app_engine=_engine(settings.database_url, settings.db_pool_size),
            system_engine=_engine(settings.database_system_url, settings.db_pool_size),
        )

    @asynccontextmanager
    async def tenant_session(self, company_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """One transaction as the RLS-restricted app role, scoped to ``company_id``; commits on success."""
        async with self._app_sessions() as session, session.begin():
            # Transaction-local (third argument true): never leaks to the next user of a pooled connection.
            await session.execute(text("SELECT set_config('app.company_id', :cid, true)"), {"cid": str(company_id)})
            yield session

    @asynccontextmanager
    async def system_session(self) -> AsyncIterator[AsyncSession]:
        """One transaction as the system role (bypasses RLS). Only for login, registration, queue and retention."""
        async with self._system_sessions() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.app_engine.dispose()
        await self.system_engine.dispose()


class NotFound(LookupError):
    """The object does not exist for this tenant (other tenants' objects are indistinguishable from missing)."""


class TenantRepo:
    """Every read is filtered by company_id; every insert is stamped with it."""

    def __init__(self, session: AsyncSession, company_id: uuid.UUID) -> None:
        self.session = session
        self.company_id = company_id

    def select(self, model: type[M], *where: Any) -> Select[tuple[M]]:
        return select(model).where(model.company_id == self.company_id, *where)  # type: ignore[attr-defined]

    async def get(self, model: type[M], object_id: uuid.UUID, *, for_update: bool = False) -> M:
        stmt = self.select(model, model.id == object_id)  # type: ignore[attr-defined]
        if for_update:
            # Refresh an already-loaded instance: a locking read must see the latest committed values.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        obj = (await self.session.execute(stmt)).scalar_one_or_none()
        if obj is None:
            raise NotFound(f"{model.__tablename__} {object_id} not found")
        return obj

    async def find(self, model: type[M], *where: Any) -> M | None:
        return (await self.session.execute(self.select(model, *where))).scalars().first()

    async def list(
        self, model: type[M], *where: Any, order_by: Sequence[Any] = (), limit: int | None = None
    ) -> list[M]:
        stmt = self.select(model, *where).order_by(*order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    def add(self, obj: M) -> M:
        obj.company_id = self.company_id  # type: ignore[attr-defined]
        self.session.add(obj)
        return obj

    async def flush(self) -> None:
        await self.session.flush()
