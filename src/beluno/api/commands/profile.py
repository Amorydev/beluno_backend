"""The caller's own profile."""

from __future__ import annotations

from typing import Any

from beluno.api.presenters import profile_response
from beluno.contracts.errors import not_found
from beluno.contracts.iam import ProfileUpdateRequest, UserProfileResponse
from beluno.modules import account_deletion
from beluno.modules.context import CommandContext
from beluno.modules.iam import users
from beluno.modules.iam.sessions import find_session, revoke_session
from beluno.sync.commands import Command, CommandCall, EmptyPayload, required_version, version_of


async def _update(
    ctx: CommandContext, call: CommandCall, body: ProfileUpdateRequest
) -> UserProfileResponse:
    fields = body.model_fields_set
    changes = users.ProfileChanges(
        display_name=body.display_name,
        locale=body.locale,
        timezone=body.timezone,
        default_currency=body.default_currency,
        clear_locale="locale" in fields and body.locale is None,
        clear_timezone="timezone" in fields and body.timezone is None,
        clear_default_currency="default_currency" in fields and body.default_currency is None,
    )
    user = await users.update_profile(ctx, changes, required_version(call))
    return profile_response(user)


async def _revoke_session(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    """Sign a device out remotely; its refresh and access tokens stop working immediately."""

    target = await find_session(ctx, call.id("session_id"))
    if target is None or target.user_id != ctx.require_actor().user_id:
        raise not_found()
    await revoke_session(ctx, target, reason="user_revoked")


async def _delete_account(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    """Delete the caller's account (recent sign-in); they become "Former member"."""

    await account_deletion.delete_account(ctx)


PROFILE_UPDATE = Command(
    name="profile.update",
    payload_model=ProfileUpdateRequest,
    response_model=UserProfileResponse,
    handler=_update,
    versioned=True,
    etag=version_of,
)

SESSION_REVOKE = Command(
    name="session.revoke",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_revoke_session,
    target_fields=("session_id",),
    status=204,
)

PROFILE_DELETE = Command(
    name="profile.delete",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_delete_account,
    status=204,
)

COMMANDS: list[Command[Any, Any]] = [PROFILE_UPDATE, SESSION_REVOKE, PROFILE_DELETE]
