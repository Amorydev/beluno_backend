"""Turn a verified identity into a session, a step-up, or a guest upgrade.

* No caller: sign in (creating the account on first use) and start a session.
* Registered caller: the identity must be theirs; refresh ``authenticated_at``
  on the current session (step-up for sensitive actions). An unlinked
  Google/Apple identity is linked to the account instead (recent sign-in required).
* Guest caller: if the identity is new, upgrade the guest in place (same user
  ID). If it belongs to an existing account, hand the guest's plan participation
  to that account through ``transfer_guest`` and retire the guest.

Plan participation lives in the plans module, so it is injected rather than
imported to keep ``iam`` free of plan dependencies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from beluno.auth import AuthenticatedActor
from beluno.contracts.errors import (
    BelunoError,
    authentication_failed,
    conflict,
    step_up_required,
)
from beluno.db.models.iam import User
from beluno.modules.context import CommandContext
from beluno.modules.iam import users
from beluno.modules.iam.external_identity import IdentityProvider, VerifiedIdentity
from beluno.modules.iam.sessions import (
    DeviceInfo,
    IssuedTokens,
    current_session_tokens,
    mark_reauthenticated,
    revoke_all_sessions,
    start_session,
)

# (ctx, guest_user_id, target_user_id, merge_existing). ``target == guest`` means
# an in-place upgrade; otherwise participation moves to an existing account.
GuestTransfer = Callable[[CommandContext, UUID, UUID, bool], Awaitable[None]]


@dataclass(frozen=True)
class AuthenticationRequest:
    identity: VerifiedIdentity
    device: DeviceInfo
    merge_guest_participations: bool = False


async def authenticate(
    ctx: CommandContext,
    request: AuthenticationRequest,
    transfer_guest: GuestTransfer,
) -> IssuedTokens:
    actor = ctx.actor
    if actor is None:
        return await _sign_in(ctx, request)
    if actor.is_guest:
        return await _upgrade_or_claim_guest(ctx, actor, request, transfer_guest)
    return await _reauthenticate(ctx, actor, request)


async def _resolve_account(ctx: CommandContext, identity: VerifiedIdentity) -> UUID | None:
    """Find the account linked to an identity.

    An unlinked Google/Apple identity whose verified email belongs to an existing
    account is never attached automatically: provider email ownership can lapse
    (old work address, expired domain). The account holder links it explicitly
    from a recently authenticated session instead.
    """

    user_id = await users.find_user_id(ctx, identity)
    if user_id is not None or identity.provider is IdentityProvider.EMAIL:
        return user_id
    email_taken = (
        identity.email is not None
        and identity.email_verified
        and await users.find_user_id_by_email(ctx, identity.email) is not None
    )
    if email_taken:
        raise conflict(
            "ACCOUNT_LINK_REQUIRED",
            "An account already uses this email",
            "Sign in with your existing method, then link this sign-in option",
        )
    return None


async def _load_active_user(ctx: CommandContext, user_id: UUID) -> User:
    await ctx.act_as(user_id)
    user = await users.load_user(ctx, user_id, for_update=True)
    if user.status != "active" or user.kind != users.REGISTERED:
        raise authentication_failed()
    return user


async def _sign_in(ctx: CommandContext, request: AuthenticationRequest) -> IssuedTokens:
    identity = request.identity
    user_id = await _resolve_account(ctx, identity)
    if user_id is None:
        try:
            async with ctx.session.begin_nested():
                user = await users.create_registered_user(ctx, identity)
        except IntegrityError:
            # A concurrent first sign-in created the account; use it.
            user_id = await _resolve_account(ctx, identity)
            if user_id is None:
                raise
            user = await _load_active_user(ctx, user_id)
    else:
        user = await _load_active_user(ctx, user_id)
    await users.touch_identity(ctx, identity)
    return await start_session(
        ctx, user, auth_method=identity.provider.value, device=request.device
    )


async def _reauthenticate(
    ctx: CommandContext,
    actor: AuthenticatedActor,
    request: AuthenticationRequest,
) -> IssuedTokens:
    identity = request.identity
    owner_id = await users.find_user_id(ctx, identity)
    if owner_id is None and identity.provider is not IdentityProvider.EMAIL:
        return await _link_identity(ctx, actor, identity)
    if owner_id != actor.user_id:
        raise BelunoError(
            status=403,
            code="REAUTHENTICATION_MISMATCH",
            title="Sign in with the account that is already signed in",
        )
    user = await users.load_user(ctx, actor.user_id)
    await users.touch_identity(ctx, identity)
    return await mark_reauthenticated(
        ctx, user, actor.session_id, auth_method=identity.provider.value
    )


async def _link_identity(
    ctx: CommandContext,
    actor: AuthenticatedActor,
    identity: VerifiedIdentity,
) -> IssuedTokens:
    """Attach a new Google/Apple sign-in to the caller's account (recent sign-in required)."""

    if not ctx.step_up_is_fresh:
        raise step_up_required()
    user = await users.load_user(ctx, actor.user_id)
    await users.link_identity(ctx, user.id, identity)
    await users.record_identity_linked(ctx, user, identity.provider.value)
    return await current_session_tokens(ctx, user, actor.session_id)


async def _upgrade_or_claim_guest(
    ctx: CommandContext,
    actor: AuthenticatedActor,
    request: AuthenticationRequest,
    transfer_guest: GuestTransfer,
) -> IssuedTokens:
    identity = request.identity
    guest = await users.load_user(ctx, actor.user_id, for_update=True)
    existing_user_id = await _resolve_account(ctx, identity)
    if existing_user_id is None:
        await users.upgrade_guest(ctx, guest, identity)
        await transfer_guest(ctx, guest.id, guest.id, False)
        return await mark_reauthenticated(
            ctx, guest, actor.session_id, auth_method=identity.provider.value
        )
    await transfer_guest(ctx, guest.id, existing_user_id, request.merge_guest_participations)
    await users.retire_merged_guest(ctx, guest, existing_user_id)
    await revoke_all_sessions(ctx, guest.id, reason="account_merged")
    user = await _load_active_user(ctx, existing_user_id)
    await users.touch_identity(ctx, identity)
    return await start_session(
        ctx, user, auth_method=identity.provider.value, device=request.device
    )
