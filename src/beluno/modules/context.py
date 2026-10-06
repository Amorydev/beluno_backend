"""Process runtime and the per-transaction command context shared by every module.

``open_context`` is the single entrypoint for domain work: it opens one
transaction, sets the RLS actor, and re-validates the caller's session and
account against PostgreSQL so a revoked device or disabled account loses access
immediately even while its access token is still unexpired. Audit and change
records buffered by the command are written just before the transaction commits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beluno.auth import AccessTokenCodec, AuthenticatedActor
from beluno.config import Settings
from beluno.contracts.errors import authentication_failed, authentication_unavailable
from beluno.db.models.iam import AuthSession, User
from beluno.db.roles import set_actor_context
from beluno.db.session import Database
from beluno.modules.sync_audit.recorder import flush_pending_records
from beluno.observability.context import get_request_id
from beluno.token_hashing import TokenHasher

if TYPE_CHECKING:
    from beluno.modules.iam.external_identity import ExternalIdentityVerifier


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Runtime:
    """Process-wide collaborators; built once per API or worker process."""

    settings: Settings
    database: Database
    tokens: AccessTokenCodec
    hasher: TokenHasher | None
    identity_verifier: ExternalIdentityVerifier
    clock: Callable[[], datetime] = field(default=utc_now)

    def require_hasher(self) -> TokenHasher:
        if self.hasher is None:
            raise authentication_unavailable()
        return self.hasher


@dataclass
class CommandContext:
    runtime: Runtime
    session: AsyncSession
    now: datetime
    actor: AuthenticatedActor | None
    request_id: str | None
    # Background work done for a user without a session (e.g. series materialization).
    on_behalf_of: UUID | None = None
    # Set when the command carries an idempotency key; change rows point back to it.
    operation_id: UUID | None = None
    # Audit and change rows recorded by this command, written just before commit.
    pending_audit: list[dict[str, Any]] = field(default_factory=list)
    pending_changes: list[dict[str, Any]] = field(default_factory=list)
    savepoint_depth: int = 0

    @property
    def settings(self) -> Settings:
        return self.runtime.settings

    def require_actor(self) -> AuthenticatedActor:
        if self.actor is None:
            raise authentication_failed()
        return self.actor

    @property
    def step_up_is_fresh(self) -> bool:
        if self.actor is None:
            return False
        max_age = timedelta(seconds=self.settings.auth_step_up_max_age_seconds)
        return self.now - self.actor.authenticated_at <= max_age

    async def act_as(self, user_id: UUID | None) -> None:
        """Switch the RLS actor inside this transaction (sign-in and claim flows)."""

        await set_actor_context(self.session, user_id)

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """A nested transaction whose rollback also drops the records made inside it.

        Use this instead of ``session.begin_nested()`` around anything that may call
        ``record_mutation``; records otherwise wait in memory until commit and would
        outlive the rolled-back rows they describe.
        """

        audit_mark, change_mark = len(self.pending_audit), len(self.pending_changes)
        self.savepoint_depth += 1
        try:
            async with self.session.begin_nested():
                yield
        except BaseException:
            del self.pending_audit[audit_mark:]
            del self.pending_changes[change_mark:]
            raise
        finally:
            self.savepoint_depth -= 1


async def load_current_actor(
    session: AsyncSession,
    actor: AuthenticatedActor,
    now: datetime,
) -> AuthenticatedActor:
    """Return the actor as PostgreSQL sees it now, or reject the request."""

    row = (
        await session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.id == actor.session_id, AuthSession.user_id == actor.user_id)
        )
    ).one_or_none()
    if row is None:
        raise authentication_failed()
    auth_session, user = row
    if (
        auth_session.revoked_at is not None
        or now >= auth_session.idle_expires_at
        or now >= auth_session.absolute_expires_at
        or user.status != "active"
    ):
        raise authentication_failed()
    return AuthenticatedActor(
        user_id=user.id,
        session_id=auth_session.id,
        authenticated_at=auth_session.authenticated_at,
        is_guest=user.kind == "guest",
    )


@asynccontextmanager
async def open_context(
    runtime: Runtime,
    actor: AuthenticatedActor | None = None,
) -> AsyncIterator[CommandContext]:
    now = runtime.clock()
    async with runtime.database.transaction(actor.user_id if actor else None) as session:
        current_actor = await load_current_actor(session, actor, now) if actor else None
        ctx = CommandContext(
            runtime=runtime,
            session=session,
            now=now,
            actor=current_actor,
            request_id=get_request_id(),
        )
        yield ctx
        await flush_pending_records(ctx)
