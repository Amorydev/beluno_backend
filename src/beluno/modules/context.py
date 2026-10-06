"""Process runtime and the per-transaction command context shared by every module.

``open_context`` is the single entrypoint for domain work: it opens one
transaction, sets the RLS actor, and re-validates the caller's session and
account against PostgreSQL so a revoked device or disabled account loses access
immediately even while its access token is still unexpired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beluno.auth import AccessTokenCodec, AuthenticatedActor
from beluno.config import Settings
from beluno.contracts.errors import authentication_failed, authentication_unavailable
from beluno.db.models.iam import AuthSession, User
from beluno.db.roles import set_actor_context
from beluno.db.session import Database
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
        yield CommandContext(
            runtime=runtime,
            session=session,
            now=now,
            actor=current_actor,
            request_id=get_request_id(),
        )
