"""Plan commands: lifecycle, participants, RSVP, ownership, duplication, invite links."""

from __future__ import annotations

from typing import Any

from beluno.api.presenters import (
    invite_response,
    participant_response,
    plan_response,
    seed_of,
    timing_input,
)
from beluno.authorization.policy import PlanRole, PlanState
from beluno.contracts.invites import InviteResponse
from beluno.contracts.plans import (
    JoinRequestDecision,
    ParticipantAddRequest,
    ParticipantResponse,
    ParticipantRoleRequest,
    PlanCreateRequest,
    PlanDuplicateRequest,
    PlanOwnershipTransferRequest,
    PlanResponse,
    PlanStateChangeRequest,
    PlanUpdateRequest,
    RsvpRequest,
)
from beluno.modules.context import CommandContext
from beluno.modules.iam import rate_limits
from beluno.modules.plans import duplication, invites, service
from beluno.modules.plans import participants as participant_service
from beluno.sync.commands import Command, CommandCall, EmptyPayload, required_version, version_of


async def _create(ctx: CommandContext, call: CommandCall, body: PlanCreateRequest) -> PlanResponse:
    draft = service.PlanDraft(
        plan_id=body.id,
        title=body.title,
        kind=body.kind,
        state=PlanState(body.state),
        timing=timing_input(body.timing),
        base_currency=body.base_currency,
        description=body.description,
        location_label=body.location_label,
        seeds=tuple(seed_of(seed) for seed in body.participants),
    )
    return plan_response(await service.create_plan(ctx, draft))


async def _update(ctx: CommandContext, call: CommandCall, body: PlanUpdateRequest) -> PlanResponse:
    fields = body.model_fields_set
    changes = service.PlanChanges(
        title=body.title,
        kind=body.kind,
        timing=timing_input(body.timing) if body.timing else None,
        base_currency=body.base_currency,
        description=body.description if "description" in fields else service.UNSET,
        location_label=body.location_label if "location_label" in fields else service.UNSET,
    )
    view = await service.update_plan(ctx, call.id("plan_id"), required_version(call), changes)
    return plan_response(view)


async def _change_state(
    ctx: CommandContext, call: CommandCall, body: PlanStateChangeRequest
) -> PlanResponse:
    view = await service.change_state(
        ctx, call.id("plan_id"), required_version(call), PlanState(body.state)
    )
    return plan_response(view)


async def _schedule_deletion(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> PlanResponse:
    view = await service.schedule_deletion(ctx, call.id("plan_id"), required_version(call))
    return plan_response(view)


async def _restore(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> PlanResponse:
    view = await service.restore_plan(ctx, call.id("plan_id"), required_version(call))
    return plan_response(view)


async def _transfer_ownership(
    ctx: CommandContext, call: CommandCall, body: PlanOwnershipTransferRequest
) -> PlanResponse:
    access = await participant_service.transfer_ownership(
        ctx, call.id("plan_id"), body.new_owner_participant_id, required_version(call)
    )
    return plan_response(service.PlanView(plan=access.plan, participant=access.participant))


async def _add_participant(
    ctx: CommandContext, call: CommandCall, body: ParticipantAddRequest
) -> ParticipantResponse:
    participant = await participant_service.add_participant(ctx, call.id("plan_id"), seed_of(body))
    return participant_response(participant)


async def _change_participant_role(
    ctx: CommandContext, call: CommandCall, body: ParticipantRoleRequest
) -> ParticipantResponse:
    participant = await participant_service.change_role(
        ctx,
        call.id("plan_id"),
        call.id("participant_id"),
        PlanRole(body.role),
        required_version(call),
    )
    return participant_response(participant)


async def _remove_participant(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await participant_service.remove_participant(ctx, call.id("plan_id"), call.id("participant_id"))


async def _review_join_request(
    ctx: CommandContext, call: CommandCall, body: JoinRequestDecision
) -> ParticipantResponse:
    participant = await participant_service.review_join_request(
        ctx, call.id("plan_id"), call.id("participant_id"), body.approve
    )
    return participant_response(participant)


async def _leave(ctx: CommandContext, call: CommandCall, body: EmptyPayload) -> None:
    await participant_service.leave_plan(ctx, call.id("plan_id"))


async def _rsvp(ctx: CommandContext, call: CommandCall, body: RsvpRequest) -> ParticipantResponse:
    participant = await participant_service.respond_rsvp(ctx, call.id("plan_id"), body.status)
    return participant_response(participant)


async def _duplicate(
    ctx: CommandContext, call: CommandCall, body: PlanDuplicateRequest
) -> PlanResponse:
    options = duplication.DuplicateOptions(
        title=body.title,
        timing=timing_input(body.timing),
        participant_ids=tuple(body.participant_ids) if body.participant_ids is not None else None,
        include_description=body.include_description,
        include_location=body.include_location,
    )
    return plan_response(await duplication.duplicate_plan(ctx, call.id("plan_id"), options))


async def _revoke_invite(
    ctx: CommandContext, call: CommandCall, body: EmptyPayload
) -> InviteResponse:
    invite = await invites.revoke_invite(ctx, call.id("plan_id"), call.id("invite_id"))
    return invite_response(invite)


PLAN_CREATE = Command(
    name="plan.create",
    payload_model=PlanCreateRequest,
    response_model=PlanResponse,
    handler=_create,
    status=201,
    etag=version_of,
)
PLAN_UPDATE = Command(
    name="plan.update",
    payload_model=PlanUpdateRequest,
    response_model=PlanResponse,
    handler=_update,
    target_fields=("plan_id",),
    versioned=True,
    etag=version_of,
)
PLAN_CHANGE_STATE = Command(
    name="plan.change_state",
    payload_model=PlanStateChangeRequest,
    response_model=PlanResponse,
    handler=_change_state,
    target_fields=("plan_id",),
    versioned=True,
    etag=version_of,
)
PLAN_SCHEDULE_DELETION = Command(
    name="plan.schedule_deletion",
    payload_model=EmptyPayload,
    response_model=PlanResponse,
    handler=_schedule_deletion,
    target_fields=("plan_id",),
    versioned=True,
    etag=version_of,
)
PLAN_RESTORE = Command(
    name="plan.restore",
    payload_model=EmptyPayload,
    response_model=PlanResponse,
    handler=_restore,
    target_fields=("plan_id",),
    versioned=True,
    etag=version_of,
)
PLAN_TRANSFER_OWNERSHIP = Command(
    name="plan.transfer_ownership",
    payload_model=PlanOwnershipTransferRequest,
    response_model=PlanResponse,
    handler=_transfer_ownership,
    target_fields=("plan_id",),
    versioned=True,
    etag=version_of,
)
PLAN_PARTICIPANT_ADD = Command(
    name="plan.participant.add",
    payload_model=ParticipantAddRequest,
    response_model=ParticipantResponse,
    handler=_add_participant,
    target_fields=("plan_id",),
    status=201,
)
PLAN_PARTICIPANT_CHANGE_ROLE = Command(
    name="plan.participant.change_role",
    payload_model=ParticipantRoleRequest,
    response_model=ParticipantResponse,
    handler=_change_participant_role,
    target_fields=("plan_id", "participant_id"),
    versioned=True,
)
PLAN_PARTICIPANT_REMOVE = Command(
    name="plan.participant.remove",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_remove_participant,
    target_fields=("plan_id", "participant_id"),
    status=204,
)
PLAN_PARTICIPANT_REVIEW = Command(
    name="plan.participant.review",
    payload_model=JoinRequestDecision,
    response_model=ParticipantResponse,
    handler=_review_join_request,
    target_fields=("plan_id", "participant_id"),
)
PLAN_LEAVE = Command(
    name="plan.leave",
    payload_model=EmptyPayload,
    response_model=None,
    handler=_leave,
    target_fields=("plan_id",),
    status=204,
)
PLAN_RSVP = Command(
    name="plan.rsvp",
    payload_model=RsvpRequest,
    response_model=ParticipantResponse,
    handler=_rsvp,
    target_fields=("plan_id",),
)
PLAN_DUPLICATE = Command(
    name="plan.duplicate",
    payload_model=PlanDuplicateRequest,
    response_model=PlanResponse,
    handler=_duplicate,
    target_fields=("plan_id",),
    status=201,
    rate_limit=rate_limits.DUPLICATION_PER_USER,
    etag=version_of,
)
PLAN_INVITE_REVOKE = Command(
    name="plan.invite.revoke",
    payload_model=EmptyPayload,
    response_model=InviteResponse,
    handler=_revoke_invite,
    target_fields=("plan_id", "invite_id"),
)

COMMANDS: list[Command[Any, Any]] = [
    PLAN_CREATE,
    PLAN_UPDATE,
    PLAN_CHANGE_STATE,
    PLAN_SCHEDULE_DELETION,
    PLAN_RESTORE,
    PLAN_TRANSFER_OWNERSHIP,
    PLAN_PARTICIPANT_ADD,
    PLAN_PARTICIPANT_CHANGE_ROLE,
    PLAN_PARTICIPANT_REMOVE,
    PLAN_PARTICIPANT_REVIEW,
    PLAN_LEAVE,
    PLAN_RSVP,
    PLAN_DUPLICATE,
    PLAN_INVITE_REVOKE,
]
