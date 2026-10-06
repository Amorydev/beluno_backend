"""Explicit SQLAlchemy asyncio session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from beluno.config import Settings
from beluno.db.roles import set_actor_context

UTC_SESSION_OPTION = "-c timezone=UTC"


def session_options(dsn: str) -> str:
    """Server options for every pooled connection: the DSN's own, then a UTC session zone.

    ``timestamptz`` values come back in the session time zone. Pinning it to UTC
    makes a row serialize identically in the write response, REST reads, and
    sync pulls, whatever ``TimeZone`` the server or role defaults to.
    """

    configured = make_url(dsn).query.get("options")
    existing = " ".join(configured) if isinstance(configured, tuple) else configured
    return f"{existing} {UTC_SESSION_OPTION}" if existing else UTC_SESSION_OPTION


class Database:
    """Owns one process connection pool; sessions are never shared across requests or jobs."""

    def __init__(self, settings: Settings, *, dsn: str | None = None) -> None:
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        active_dsn = dsn or settings.api_database_dsn
        if active_dsn:
            self._engine = create_async_engine(
                active_dsn,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                pool_recycle=1_800,
                # Statement parameters (amounts, notes, descriptions) never reach error text.
                hide_parameters=True,
                connect_args={"options": session_options(active_dsn)},
            )
            self._session_factory = async_sessionmaker(
                self._engine,
                autoflush=False,
                expire_on_commit=False,
            )

    @classmethod
    def for_worker(cls, settings: Settings) -> Database:
        return cls(settings, dsn=settings.worker_database_dsn)

    @property
    def configured(self) -> bool:
        return self._engine is not None and self._session_factory is not None

    async def check(self) -> bool:
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    @asynccontextmanager
    async def transaction(self, actor_id: UUID | str | None = None) -> AsyncIterator[AsyncSession]:
        """Yield one transaction with transaction-local RLS actor context.

        The transaction commits when the block exits normally and rolls back on
        any exception, so callers never observe a response for uncommitted work.
        """

        if self._session_factory is None:
            raise RuntimeError("Database is not configured")
        async with self._session_factory() as session, session.begin():
            await set_actor_context(session, str(actor_id) if actor_id else None)
            yield session

    async def session(self) -> AsyncIterator[AsyncSession]:
        """Compatibility helper; new application services use ``transaction``."""

        async with self.transaction() as session:
            yield session
