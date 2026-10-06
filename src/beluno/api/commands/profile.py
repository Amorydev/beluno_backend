"""The caller's own profile."""

from __future__ import annotations

from typing import Any

from beluno.api.commands.groups import required_version
from beluno.api.presenters import profile_response
from beluno.contracts.errors import not_found
from beluno.contracts.iam import ProfileUpdateRequest, UserProfileResponse
from beluno.modules.context import CommandContext
from beluno.modules.groups.service import refresh_member_name
from beluno.modules.iam import users
from beluno.modules.iam.sessions import find_session, revoke_session
from beluno.sync.commands import Command, CommandCall, EmptyPayload, version_of


async def _update(
    ctx: CommandContext, call: CommandCall, body: ProfileUpdateRequest
) -> UserProfileResponse:
    fields = body.model_fields_set
    changes = users.ProfileChanges(
        display_name=body.display_name,
        locale=body.locale,
        timezone=body.timezone,
        clear_locale="locale" in fields and body.locale is None,
        clear_timezone="timezone" in fields and body.timezone is None,
    )
    user = await users.update_profile(ctx, changes, required_version(call))
    if changes.display_name is not None:
        await refresh_member_name(ctx, user)
    return profile_response(user)


async def _revoke_session(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    """Sign a device out remotely; its refresh and access tokens stop working immediately."""

    target = await find_session(ctx, call.id("session_id"))
    if target is None or target.user_id != ctx.require_actor().user_id:
        raise not_found()
    await revoke_session(ctx, target, reason="user_revoked")


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

COMMANDS: list[Command[Any, Any]] = [PROFILE_UPDATE, SESSION_REVOKE]
