"""Render domain rows into public response contracts.

Shared by the REST routes, the command handlers (which render inside the
transaction so an idempotent replay can return the same body), and the sync
feed. ``present_current`` renders the row behind a stale-version conflict.
"""

from __future__ import annotations

from pydantic import BaseModel

from beluno.authorization.access import find_user_participant
from beluno.authorization.policy import AccessState, PlanRole
from beluno.contracts.iam import DeviceRequest, TokenResponse, UserProfileResponse
from beluno.contracts.invites import CreatedInviteResponse, InviteResponse
from beluno.contracts.plans import (
    ParticipantResponse,
    ParticipantSeed,
    PlanResponse,
    PlanTiming,
)
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan, PlanInvite, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.iam.sessions import DeviceInfo, IssuedTokens
from beluno.modules.plans import service as plan_service
from beluno.modules.plans.participants import Seed


def profile_response(user: User) -> UserProfileResponse:
    return UserProfileResponse.model_validate(
        {
            "id": user.id,
            "kind": user.kind,
            "display_name": user.display_name,
            "email": user.email,
            "locale": user.locale,
            "timezone": user.timezone,
            "default_currency": user.default_currency,
            "version": user.version,
        }
    )


def token_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        expires_at=tokens.access_token_expires_at,
        refresh_token=tokens.refresh_token,
        session_id=tokens.session_id,
        user=profile_response(tokens.user),
    )


def device_info(device: DeviceRequest | None) -> DeviceInfo:
    if device is None:
        return DeviceInfo()
    return DeviceInfo(
        client_device_id=device.client_device_id,
        label=device.label,
        platform=device.platform,
        app_version=device.app_version,
    )


def participant_response(participant: PlanParticipant) -> ParticipantResponse:
    return ParticipantResponse.model_validate(
        {
            "id": participant.id,
            "plan_id": participant.plan_id,
            "identity_kind": participant.identity_kind,
            "user_id": participant.user_id,
            "display_name": participant.display_name,
            "role": participant.role,
            "access_state": participant.access_state,
            "rsvp_status": participant.rsvp_status,
            "rsvp_updated_at": participant.rsvp_updated_at,
            "default_share": participant.default_share,
            "avatar_color": participant.avatar_color,
            "capabilities": sorted(participant.capabilities),
            "merged_into_participant_id": participant.merged_into_participant_id,
            "joined_at": participant.joined_at,
            "version": participant.version,
        }
    )


def timing_contract(timing: plan_service.Timing) -> PlanTiming:
    return PlanTiming.model_validate(timing.__dict__)


def timing_input(timing: PlanTiming) -> plan_service.Timing:
    return plan_service.Timing(
        mode=timing.mode,
        start_date=timing.start_date,
        end_date=timing.end_date,
        starts_at=timing.starts_at,
        ends_at=timing.ends_at,
        timezone=timing.timezone,
    )


def plan_response(view: plan_service.PlanView) -> PlanResponse:
    plan = view.plan
    return PlanResponse.model_validate(
        {
            "id": plan.id,
            "type": plan.type,
            "title": plan.title,
            "activity": plan.activity,
            "state": plan.state,
            "timing": timing_contract(plan_service.timing_of(plan)),
            "base_currency": plan.base_currency,
            "destinations": plan.destinations,
            "pass_color": plan.pass_color,
            "expected_size": plan.expected_size,
            "description": plan.description,
            "location_label": plan.location_label,
            "deletion_scheduled_at": plan.deletion_scheduled_at,
            "my_participant": participant_response(view.participant) if view.participant else None,
            "version": plan.version,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
        }
    )


def seed_of(body: ParticipantSeed) -> Seed:
    return Seed(
        user_id=body.user_id,
        placeholder_name=body.placeholder_name,
        role=PlanRole(body.role),
        participant_id=body.id,
    )


def invite_response(invite: PlanInvite) -> InviteResponse:
    return InviteResponse.model_validate(
        {
            "id": invite.id,
            "kind": "plan",
            "purpose": invite.purpose,
            "role": invite.role,
            "allow_guests": invite.allow_guests,
            "requires_approval": invite.requires_approval,
            "email_bound": invite.intended_email_hash is not None,
            "target_participant_id": invite.target_participant_id,
            "max_uses": invite.max_uses,
            "use_count": invite.use_count,
            "expires_at": invite.expires_at,
            "state": invite.state,
            "created_at": invite.created_at,
        }
    )


def created_invite_response(invite: PlanInvite, token: str) -> CreatedInviteResponse:
    return CreatedInviteResponse(**invite_response(invite).model_dump(), token=token)


async def present_current(ctx: CommandContext, entity: object) -> BaseModel | None:
    """The caller's view of the row whose version was stale (rendered in-transaction)."""

    actor = ctx.require_actor()
    if isinstance(entity, Plan):
        participant = await find_user_participant(ctx, entity.id, actor.user_id)
        if participant is not None and participant.access_state != AccessState.ACTIVE.value:
            participant = None
        return plan_response(plan_service.PlanView(plan=entity, participant=participant))
    if isinstance(entity, PlanParticipant):
        return participant_response(entity)
    if isinstance(entity, User):
        return profile_response(entity)
    return None
