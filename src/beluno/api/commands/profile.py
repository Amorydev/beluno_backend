"""The caller's own profile."""

from __future__ import annotations

from typing import Any

from beluno.api.commands.groups import required_version
from beluno.api.presenters import profile_response
from beluno.contracts.iam import ProfileUpdateRequest, UserProfileResponse
from beluno.modules.context import CommandContext
from beluno.modules.groups.service import refresh_member_name
from beluno.modules.iam import users
from beluno.sync.commands import Command, CommandCall, version_of


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


PROFILE_UPDATE = Command(
    name="profile.update",
    payload_model=ProfileUpdateRequest,
    response_model=UserProfileResponse,
    handler=_update,
    versioned=True,
    etag=version_of,
)

COMMANDS: list[Command[Any, Any]] = [PROFILE_UPDATE]
