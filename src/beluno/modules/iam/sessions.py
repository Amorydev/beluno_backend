"""Device sessions, access-token issuance, and rotating refresh tokens.

Refresh tokens are opaque, stored only as HMAC digests, and single-use. Presenting
a consumed token is treated as theft and revokes the whole session, except inside
a short grace window that lets a client retry after losing the refresh response;
that retry supersedes the unused replacement so only one live token remains.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from beluno.contracts.errors import authentication_failed
from beluno.db.ids import new_id
from beluno.db.models.iam import AuthSession, RefreshToken, User
from beluno.modules.context import CommandContext
from beluno.modules.sync_audit.recorder import ChangeScope, record_mutation
from beluno.observability.setup import logger, safe_extra
from beluno.token_hashing import new_secret_token

REFRESH_TOKEN_PURPOSE = "refresh_token"
security_log = logger("beluno.security")


@dataclass(frozen=True)
class DeviceInfo:
    client_device_id: str | None = None
    label: str | None = None
    platform: str | None = None
    app_version: str | None = None


@dataclass(frozen=True)
class IssuedTokens:
    user: User
    session_id: UUID
    access_token: str
    access_token_expires_at: datetime
    refresh_token: str | None


def _issue_tokens(
    ctx: CommandContext,
    user: User,
    auth_session: AuthSession,
    *,
    refresh_token: str | None,
) -> IssuedTokens:
    """Mint an access token for ``auth_session`` and bundle it with ``refresh_token``."""

    access = ctx.runtime.tokens.issue(
        user_id=user.id,
        session_id=auth_session.id,
        authenticated_at=auth_session.authenticated_at,
        is_guest=user.kind == "guest",
        now=ctx.now,
    )
    return IssuedTokens(
        user=user,
        session_id=auth_session.id,
        access_token=access.token,
        access_token_expires_at=access.expires_at,
        refresh_token=refresh_token,
    )


async def start_session(
    ctx: CommandContext,
    user: User,
    *,
    auth_method: str,
    device: DeviceInfo,
) -> IssuedTokens:
    settings = ctx.settings
    absolute_expiry = ctx.now + timedelta(seconds=settings.auth_session_max_ttl_seconds)
    auth_session = AuthSession(
        id=new_id(),
        user_id=user.id,
        auth_method=auth_method,
        authenticated_at=ctx.now,
        client_device_id=device.client_device_id,
        device_label=device.label,
        platform=device.platform,
        app_version=device.app_version,
        created_at=ctx.now,
        last_seen_at=ctx.now,
        idle_expires_at=_idle_expiry(ctx, absolute_expiry),
        absolute_expires_at=absolute_expiry,
        revoked_at=None,
        revoked_reason=None,
    )
    ctx.session.add(auth_session)
    await ctx.session.flush()
    refresh_token = await _issue_refresh_token(ctx, auth_session)
    tokens = _issue_tokens(ctx, user, auth_session, refresh_token=refresh_token)
    await record_mutation(
        ctx,
        action="session.started",
        entity_type="session",
        entity_id=auth_session.id,
        entity_version=1,
        scope=ChangeScope.USER,
        scope_id=user.id,
        metadata={"auth_method": auth_method, "platform": device.platform},
        actor_user_id=user.id,
    )
    return tokens


async def refresh_session(ctx: CommandContext, raw_token: str) -> IssuedTokens | None:
    """Rotate a refresh token. ``None`` means rejected; reuse revocations still commit."""

    hasher = ctx.runtime.require_hasher()
    token_hash = hasher.digest(REFRESH_TOKEN_PURPOSE, raw_token)
    presented = (
        await ctx.session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
    ).scalar_one_or_none()
    if presented is None:
        return None
    auth_session = (
        await ctx.session.execute(
            select(AuthSession).where(AuthSession.id == presented.session_id).with_for_update()
        )
    ).scalar_one()
    if not _session_is_live(ctx, auth_session) or presented.expires_at <= ctx.now:
        return None
    if presented.consumed_at is not None and not await _supersede_within_grace(ctx, presented):
        await revoke_session(ctx, auth_session, reason="refresh_reuse")
        security_log.warning(
            "refresh token reuse detected",
            **safe_extra(event="refresh_reuse", session_id=str(auth_session.id)),
        )
        return None
    await ctx.act_as(auth_session.user_id)
    user = await ctx.session.get(User, auth_session.user_id)
    if user is None or user.status != "active":
        return None
    presented.consumed_at = presented.consumed_at or ctx.now
    replacement = await _issue_refresh_token(ctx, auth_session)
    presented.replaced_by_hash = hasher.digest(REFRESH_TOKEN_PURPOSE, replacement)
    auth_session.last_seen_at = ctx.now
    auth_session.idle_expires_at = _idle_expiry(ctx, auth_session.absolute_expires_at)
    return _issue_tokens(ctx, user, auth_session, refresh_token=replacement)


async def mark_reauthenticated(
    ctx: CommandContext,
    user: User,
    session_id: UUID,
    *,
    auth_method: str,
) -> IssuedTokens:
    """Record a fresh sign-in on the current session (step-up) and mint a new access token."""

    auth_session = (
        await ctx.session.execute(
            select(AuthSession).where(AuthSession.id == session_id).with_for_update()
        )
    ).scalar_one()
    auth_session.authenticated_at = ctx.now
    auth_session.auth_method = auth_method
    auth_session.last_seen_at = ctx.now
    await ctx.session.flush()
    tokens = _issue_tokens(ctx, user, auth_session, refresh_token=None)
    await record_mutation(
        ctx,
        action="session.reauthenticated",
        entity_type="session",
        entity_id=auth_session.id,
        entity_version=1,
        scope=ChangeScope.USER,
        scope_id=user.id,
        metadata={"auth_method": auth_method},
    )
    return tokens


async def current_session_tokens(
    ctx: CommandContext,
    user: User,
    session_id: UUID,
) -> IssuedTokens:
    """A fresh access token for the current session without changing its auth time."""

    auth_session = (
        await ctx.session.execute(select(AuthSession).where(AuthSession.id == session_id))
    ).scalar_one()
    return _issue_tokens(ctx, user, auth_session, refresh_token=None)


async def list_live_sessions(ctx: CommandContext, user_id: UUID) -> list[AuthSession]:
    rows = await ctx.session.execute(
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > ctx.now,
            AuthSession.absolute_expires_at > ctx.now,
        )
        .order_by(AuthSession.last_seen_at.desc())
    )
    return list(rows.scalars())


async def find_session(ctx: CommandContext, session_id: UUID) -> AuthSession | None:
    return (
        await ctx.session.execute(
            select(AuthSession).where(AuthSession.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()


async def revoke_session(ctx: CommandContext, auth_session: AuthSession, *, reason: str) -> None:
    if auth_session.revoked_at is not None:
        return
    auth_session.revoked_at = ctx.now
    auth_session.revoked_reason = reason
    await ctx.session.flush()
    await record_mutation(
        ctx,
        action="session.revoked",
        entity_type="session",
        entity_id=auth_session.id,
        entity_version=1,
        scope=ChangeScope.USER,
        scope_id=auth_session.user_id,
        metadata={"reason": reason},
        operation="delete",
        actor_user_id=ctx.actor.user_id if ctx.actor else auth_session.user_id,
    )


async def revoke_other_sessions(ctx: CommandContext) -> int:
    """Sign out every other device of the caller; this one stays signed in."""

    actor = ctx.require_actor()
    # Lock every session of the person in one order: when two devices ask at once, the
    # second waits and then finds itself signed out instead of signing out the first.
    live = [
        auth_session
        for auth_session in (
            await ctx.session.execute(
                select(AuthSession)
                .where(AuthSession.user_id == actor.user_id, AuthSession.revoked_at.is_(None))
                .order_by(AuthSession.id)
                .with_for_update()
            )
        ).scalars()
        if _session_is_live(ctx, auth_session)
    ]
    if all(auth_session.id != actor.session_id for auth_session in live):
        raise authentication_failed()
    others = [auth_session for auth_session in live if auth_session.id != actor.session_id]
    for auth_session in others:
        await revoke_session(ctx, auth_session, reason="user_revoked")
    return len(others)


async def revoke_all_sessions(ctx: CommandContext, user_id: UUID, *, reason: str) -> None:
    await ctx.session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=ctx.now, revoked_reason=reason)
    )


def _session_is_live(ctx: CommandContext, auth_session: AuthSession) -> bool:
    return (
        auth_session.revoked_at is None
        and ctx.now < auth_session.idle_expires_at
        and ctx.now < auth_session.absolute_expires_at
    )


def _idle_expiry(ctx: CommandContext, absolute_expiry: datetime) -> datetime:
    idle = ctx.now + timedelta(seconds=ctx.settings.auth_session_idle_ttl_seconds)
    return min(idle, absolute_expiry)


async def _issue_refresh_token(ctx: CommandContext, auth_session: AuthSession) -> str:
    hasher = ctx.runtime.require_hasher()
    raw_token = new_secret_token()
    ctx.session.add(
        RefreshToken(
            token_hash=hasher.digest(REFRESH_TOKEN_PURPOSE, raw_token),
            session_id=auth_session.id,
            issued_at=ctx.now,
            expires_at=_idle_expiry(ctx, auth_session.absolute_expires_at),
            consumed_at=None,
            replaced_by_hash=None,
        )
    )
    await ctx.session.flush()
    return raw_token


async def _supersede_within_grace(ctx: CommandContext, presented: RefreshToken) -> bool:
    """Allow one lost-response retry: the unused replacement is retired instead."""

    grace = timedelta(seconds=ctx.settings.auth_refresh_reuse_grace_seconds)
    if presented.consumed_at is None or ctx.now - presented.consumed_at > grace:
        return False
    if presented.replaced_by_hash is None:
        return False
    replacement = (
        await ctx.session.execute(
            select(RefreshToken)
            .where(RefreshToken.token_hash == presented.replaced_by_hash)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if replacement is None or replacement.consumed_at is not None:
        return False
    replacement.consumed_at = ctx.now
    await ctx.session.flush()
    return True
