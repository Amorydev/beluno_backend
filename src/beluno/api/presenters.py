"""Render domain rows into public response contracts.

Shared by the REST routes, the command handlers (which render inside the
transaction so an idempotent replay can return the same body), and the sync
feed. ``present_current`` renders the row behind a stale-version conflict.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select

from beluno.authorization.access import find_user_participant
from beluno.authorization.policy import AccessState, PlanRole
from beluno.contracts.groups import GroupResponse, MemberResponse
from beluno.contracts.iam import DeviceRequest, TokenResponse, UserProfileResponse
from beluno.contracts.invites import CreatedInviteResponse, InviteResponse
from beluno.contracts.plans import (
    ParticipantResponse,
    ParticipantSeed,
    PlanResponse,
    PlanTiming,
    SeriesResponse,
    TravelResponse,
    TravelSegmentRequest,
    TravelSegmentResponse,
)
from beluno.db.models.groups import Group, GroupInvite, GroupMembership
from beluno.db.models.iam import User
from beluno.db.models.plans import (
    Plan,
    PlanInvite,
    PlanParticipant,
    PlanSeries,
    TravelPlanDetails,
    TravelSegment,
)
from beluno.modules.context import CommandContext
from beluno.modules.groups import service as group_service
from beluno.modules.iam.sessions import DeviceInfo, IssuedTokens
from beluno.modules.plans import service as plan_service
from beluno.modules.plans import travel
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


def group_response(view: group_service.GroupView) -> GroupResponse:
    group, membership = view.group, view.membership
    return GroupResponse.model_validate(
        {
            "id": group.id,
            "name": group.name,
            "default_currency": group.default_currency,
            "default_timezone": group.default_timezone,
            "state": group.state,
            "deletion_scheduled_at": group.deletion_scheduled_at,
            "my_role": membership.role if membership else None,
            "my_membership_state": membership.state if membership else None,
            "version": group.version,
            "created_at": group.created_at,
            "updated_at": group.updated_at,
        }
    )


def member_response(view: group_service.MemberView) -> MemberResponse:
    membership = view.membership
    return MemberResponse.model_validate(
        {
            "user_id": membership.user_id,
            "display_name": view.display_name,
            "role": membership.role,
            "state": membership.state,
            "joined_at": membership.joined_at,
            "version": membership.version,
        }
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
            "group_id": plan.group_id,
            "series_id": plan.series_id,
            "occurrence_key": plan.occurrence_key,
            "is_series_exception": plan.is_series_exception,
            "title": plan.title,
            "kind": plan.kind,
            "state": plan.state,
            "timing": timing_contract(plan_service.timing_of(plan)),
            "base_currency": plan.base_currency,
            "visibility": plan.visibility,
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


def segment_response(segment: TravelSegment) -> TravelSegmentResponse:
    return TravelSegmentResponse.model_validate(
        {
            "id": segment.id,
            "segment_type": segment.segment_type,
            "title": segment.title,
            "origin_label": segment.origin_label,
            "destination_label": segment.destination_label,
            "timing_mode": segment.timing_mode,
            "start_date": segment.start_date,
            "end_date": segment.end_date,
            "departure_local": segment.departure_local,
            "departure_timezone": segment.departure_timezone,
            "departs_at": segment.departs_at,
            "arrival_local": segment.arrival_local,
            "arrival_timezone": segment.arrival_timezone,
            "arrives_at": segment.arrives_at,
            "sort_order": segment.sort_order,
            "version": segment.version,
        }
    )


def travel_response(view: travel.TravelView) -> TravelResponse:
    return TravelResponse(
        plan_id=view.details.plan_id,
        destination_summary=view.details.destination_summary,
        notes=view.details.notes,
        version=view.details.version,
        segments=[segment_response(segment) for segment in view.segments],
    )


def segment_input(body: TravelSegmentRequest) -> travel.SegmentInput:
    return travel.SegmentInput(
        segment_id=body.id,
        segment_type=body.segment_type,
        title=body.title,
        origin_label=body.origin_label,
        destination_label=body.destination_label,
        timing_mode=body.timing_mode,
        start_date=body.start_date,
        end_date=body.end_date,
        departure_local=body.departure_local,
        departure_timezone=body.departure_timezone,
        arrival_local=body.arrival_local,
        arrival_timezone=body.arrival_timezone,
        sort_order=body.sort_order,
    )


def series_response(series: PlanSeries) -> SeriesResponse:
    return SeriesResponse.model_validate(
        {
            "id": series.id,
            "group_id": series.group_id,
            "title": series.title,
            "kind": series.kind,
            "base_currency": series.base_currency,
            "visibility": series.visibility,
            "timezone": series.timezone,
            "start_date": series.start_date,
            "local_start_time": series.local_start_time,
            "duration_minutes": series.duration_minutes,
            "recurrence_rule": series.recurrence_rule,
            "horizon_days": series.horizon_days,
            "materialized_through": series.materialized_through,
            "state": series.state,
            "version": series.version,
            "created_at": series.created_at,
        }
    )


def invite_response(invite: PlanInvite | GroupInvite) -> InviteResponse:
    if isinstance(invite, PlanInvite):
        details = {
            "kind": "plan",
            "purpose": invite.purpose,
            "allow_guests": invite.allow_guests,
            "requires_approval": invite.requires_approval,
            "email_bound": invite.intended_email_hash is not None,
            "target_participant_id": invite.target_participant_id,
        }
    else:
        details = {
            "kind": "group",
            "purpose": "join",
            "allow_guests": False,
            "requires_approval": False,
            "email_bound": False,
            "target_participant_id": None,
        }
    return InviteResponse.model_validate(
        {
            "id": invite.id,
            "role": invite.role,
            "max_uses": invite.max_uses,
            "use_count": invite.use_count,
            "expires_at": invite.expires_at,
            "state": invite.state,
            "created_at": invite.created_at,
            **details,
        }
    )


def created_invite_response(invite: PlanInvite | GroupInvite, token: str) -> CreatedInviteResponse:
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
    if isinstance(entity, Group):
        membership = await ctx.session.get(GroupMembership, (entity.id, actor.user_id))
        return group_response(group_service.GroupView(group=entity, membership=membership))
    if isinstance(entity, GroupMembership):
        user = await ctx.session.get(User, entity.user_id)
        name = user.display_name if user else group_service.FALLBACK_DISPLAY_NAME
        return member_response(group_service.MemberView(membership=entity, display_name=name))
    if isinstance(entity, PlanSeries):
        return series_response(entity)
    if isinstance(entity, TravelPlanDetails):
        segments = (
            await ctx.session.execute(
                select(TravelSegment)
                .where(TravelSegment.plan_id == entity.plan_id, TravelSegment.deleted_at.is_(None))
                .order_by(TravelSegment.sort_order, TravelSegment.id)
            )
        ).scalars()
        return travel_response(travel.TravelView(details=entity, segments=list(segments)))
    if isinstance(entity, TravelSegment):
        return segment_response(entity)
    if isinstance(entity, User):
        return profile_response(entity)
    return None
