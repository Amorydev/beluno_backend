"""Group commands: lifecycle, membership, ownership, and invite links."""

from __future__ import annotations

from typing import Any

from beluno.api.presenters import group_response, invite_response, member_response
from beluno.authorization.policy import GroupRole
from beluno.contracts.groups import (
    GroupCreateRequest,
    GroupResponse,
    GroupUpdateRequest,
    InvitationAnswerRequest,
    MemberAddRequest,
    MemberResponse,
    MemberRoleChangeRequest,
    OwnershipTransferRequest,
)
from beluno.contracts.invites import InviteResponse
from beluno.modules.context import CommandContext
from beluno.modules.groups import invites, service
from beluno.modules.iam import rate_limits
from beluno.sync.commands import Command, CommandCall, EmptyPayload, version_of


def required_version(call: CommandCall) -> int:
    assert call.expected_version is not None  # enforced by the executor for versioned commands
    return call.expected_version


async def _create(
    ctx: CommandContext, call: CommandCall, body: GroupCreateRequest
) -> GroupResponse:
    view = await service.create_group(
        ctx,
        group_id=body.id,
        name=body.name,
        default_currency=body.default_currency,
        default_timezone=body.default_timezone,
    )
    return group_response(view)


async def _update(
    ctx: CommandContext, call: CommandCall, body: GroupUpdateRequest
) -> GroupResponse:
    changes = service.GroupChanges(
        name=body.name,
        default_currency=body.default_currency,
        default_timezone=body.default_timezone,
    )
    view = await service.update_group(ctx, call.id("group_id"), required_version(call), changes)
    return group_response(view)


async def _schedule_deletion(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> GroupResponse:
    view = await service.schedule_deletion(ctx, call.id("group_id"), required_version(call))
    return group_response(view)


async def _restore(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> GroupResponse:
    view = await service.restore_group(ctx, call.id("group_id"), required_version(call))
    return group_response(view)


async def _transfer_ownership(
    ctx: CommandContext, call: CommandCall, body: OwnershipTransferRequest
) -> GroupResponse:
    view = await service.transfer_ownership(
        ctx, call.id("group_id"), body.new_owner_user_id, required_version(call)
    )
    return group_response(view)


async def _add_member(
    ctx: CommandContext, call: CommandCall, body: MemberAddRequest
) -> MemberResponse:
    view = await service.add_member(ctx, call.id("group_id"), body.user_id, GroupRole(body.role))
    return member_response(view)


async def _answer_invitation(
    ctx: CommandContext, call: CommandCall, body: InvitationAnswerRequest
) -> GroupResponse:
    view = await service.answer_invitation(ctx, call.id("group_id"), body.accept)
    return group_response(view)


async def _change_member_role(
    ctx: CommandContext, call: CommandCall, body: MemberRoleChangeRequest
) -> MemberResponse:
    view = await service.change_member_role(
        ctx, call.id("group_id"), call.id("user_id"), GroupRole(body.role), required_version(call)
    )
    return member_response(view)


async def _remove_member(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await service.remove_member(ctx, call.id("group_id"), call.id("user_id"))


async def _revoke_invite(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> InviteResponse:
    invite = await invites.revoke_invite(ctx, call.id("group_id"), call.id("invite_id"))
    return invite_response(invite)


GROUP_CREATE = Command(
    name="group.create",
    payload_model=GroupCreateRequest,
    response_model=GroupResponse,
    handler=_create,
    status=201,
    etag=version_of,
)
GROUP_UPDATE = Command(
    name="group.update",
    payload_model=GroupUpdateRequest,
    response_model=GroupResponse,
    handler=_update,
    target_fields=("group_id",),
    versioned=True,
    etag=version_of,
)
GROUP_SCHEDULE_DELETION = Command(
    name="group.schedule_deletion",
    payload_model=EmptyPayload,
    response_model=GroupResponse,
    handler=_schedule_deletion,
    target_fields=("group_id",),
    versioned=True,
    etag=version_of,
)
GROUP_RESTORE = Command(
    name="group.restore",
    payload_model=EmptyPayload,
    response_model=GroupResponse,
    handler=_restore,
    target_fields=("group_id",),
    versioned=True,
    etag=version_of,
)
GROUP_TRANSFER_OWNERSHIP = Command(
    name="group.transfer_ownership",
    payload_model=OwnershipTransferRequest,
    response_model=GroupResponse,
    handler=_transfer_ownership,
    target_fields=("group_id",),
    versioned=True,
    etag=version_of,
)
GROUP_MEMBER_ADD = Command(
    name="group.member.add",
    payload_model=MemberAddRequest,
    response_model=MemberResponse,
    handler=_add_member,
    target_fields=("group_id",),
    status=201,
    rate_limit=rate_limits.MEMBERSHIP_CHANGES_PER_USER,
)
GROUP_INVITATION_ANSWER = Command(
    name="group.invitation.answer",
    payload_model=InvitationAnswerRequest,
    response_model=GroupResponse,
    handler=_answer_invitation,
    target_fields=("group_id",),
    etag=version_of,
)
GROUP_MEMBER_CHANGE_ROLE = Command(
    name="group.member.change_role",
    payload_model=MemberRoleChangeRequest,
    response_model=MemberResponse,
    handler=_change_member_role,
    target_fields=("group_id", "user_id"),
    versioned=True,
)
GROUP_MEMBER_REMOVE = Command(
    name="group.member.remove",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_remove_member,
    target_fields=("group_id", "user_id"),
    status=204,
    rate_limit=rate_limits.MEMBERSHIP_CHANGES_PER_USER,
)
GROUP_INVITE_REVOKE = Command(
    name="group.invite.revoke",
    payload_model=EmptyPayload,
    response_model=InviteResponse,
    handler=_revoke_invite,
    target_fields=("group_id", "invite_id"),
)

COMMANDS: list[Command[Any, Any]] = [
    GROUP_CREATE,
    GROUP_UPDATE,
    GROUP_SCHEDULE_DELETION,
    GROUP_RESTORE,
    GROUP_TRANSFER_OWNERSHIP,
    GROUP_MEMBER_ADD,
    GROUP_INVITATION_ANSWER,
    GROUP_MEMBER_CHANGE_ROLE,
    GROUP_MEMBER_REMOVE,
    GROUP_INVITE_REVOKE,
]
