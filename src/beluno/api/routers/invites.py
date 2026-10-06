"""Plan invite links, plus the public preview/redeem entrypoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response, status

from beluno.api.commands import plans as plan_commands
from beluno.api.dependencies import (
    ActorDep,
    OptionalActorDep,
    RunnerDep,
    RuntimeDep,
    client_subject,
)
from beluno.api.http import IdempotencyKey, command_call, finish
from beluno.api.presenters import (
    created_invite_response,
    device_info,
    invite_response,
    participant_response,
    plan_response,
    timing_contract,
    token_response,
)
from beluno.api.problems import problem_responses
from beluno.authorization.policy import AccessState, PlanRole
from beluno.contracts.invites import (
    ClaimInviteCreateRequest,
    CreatedInviteResponse,
    InvitePreviewResponse,
    InviteResponse,
    InviteTokenRequest,
    PlanInviteCreateRequest,
    PlanInvitePreview,
    RedeemInviteRequest,
    RedeemInviteResponse,
)
from beluno.modules import invitations
from beluno.modules.context import open_context
from beluno.modules.iam import rate_limits
from beluno.modules.plans import invites as plan_invites
from beluno.modules.plans import service as plan_service
from beluno.sync.commands import EmptyPayload

router = APIRouter(tags=["invites"])

MANAGE_ERRORS = problem_responses(401, 403, 404, 409, 422, 429, 503)
PUBLIC_ERRORS = problem_responses(401, 403, 404, 409, 422, 429, 503)


async def _limit_invite_creation(runtime: RuntimeDep, actor: ActorDep) -> None:
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.INVITE_CREATION_PER_USER, str(actor.user_id)
    )


@router.post(
    "/v1/plans/{plan_id}/invites",
    status_code=status.HTTP_201_CREATED,
    response_model=CreatedInviteResponse,
    responses=MANAGE_ERRORS,
)
async def create_plan_invite(
    plan_id: UUID, body: PlanInviteCreateRequest, runtime: RuntimeDep, actor: ActorDep
) -> CreatedInviteResponse:
    await _limit_invite_creation(runtime, actor)
    settings = plan_invites.JoinInviteSettings(
        role=PlanRole(body.role),
        allow_guests=body.allow_guests,
        requires_approval=body.requires_approval,
        intended_email=body.intended_email,
        max_uses=body.max_uses,
        expires_in_hours=body.expires_in_hours,
    )
    async with open_context(runtime, actor) as ctx:
        created = await plan_invites.create_join_invite(ctx, plan_id, settings)
    return created_invite_response(created.invite, created.token)


@router.post(
    "/v1/plans/{plan_id}/participants/{participant_id}/claim-invites",
    status_code=status.HTTP_201_CREATED,
    response_model=CreatedInviteResponse,
    responses=MANAGE_ERRORS,
)
async def create_claim_invite(
    plan_id: UUID,
    participant_id: UUID,
    body: ClaimInviteCreateRequest,
    runtime: RuntimeDep,
    actor: ActorDep,
) -> CreatedInviteResponse:
    """Single-use link for the real person behind a placeholder participant."""

    await _limit_invite_creation(runtime, actor)
    async with open_context(runtime, actor) as ctx:
        created = await plan_invites.create_claim_invite(
            ctx, plan_id, participant_id, body.expires_in_hours
        )
    return created_invite_response(created.invite, created.token)


@router.get(
    "/v1/plans/{plan_id}/invites", response_model=list[InviteResponse], responses=MANAGE_ERRORS
)
async def list_plan_invites(
    plan_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> list[InviteResponse]:
    async with open_context(runtime, actor) as ctx:
        invites = await plan_invites.list_invites(ctx, plan_id)
    return [invite_response(invite) for invite in invites]


@router.delete(
    "/v1/plans/{plan_id}/invites/{invite_id}",
    response_model=InviteResponse,
    responses=MANAGE_ERRORS,
)
async def revoke_plan_invite(
    plan_id: UUID,
    invite_id: UUID,
    runner: RunnerDep,
    actor: ActorDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> InviteResponse:
    call = command_call(idempotency_key, plan_id=plan_id, invite_id=invite_id)
    result = await runner.run(actor, plan_commands.PLAN_INVITE_REVOKE, call, EmptyPayload())
    return finish(response, result)


@router.post(
    "/v1/plans/{plan_id}/invites/{invite_id}/rotate",
    status_code=status.HTTP_201_CREATED,
    response_model=CreatedInviteResponse,
    responses=MANAGE_ERRORS,
)
async def rotate_plan_invite(
    plan_id: UUID, invite_id: UUID, runtime: RuntimeDep, actor: ActorDep
) -> CreatedInviteResponse:
    await _limit_invite_creation(runtime, actor)
    async with open_context(runtime, actor) as ctx:
        created = await plan_invites.rotate_invite(ctx, plan_id, invite_id)
    return created_invite_response(created.invite, created.token)


@router.post("/v1/invites/preview", response_model=InvitePreviewResponse, responses=PUBLIC_ERRORS)
async def preview_invite(
    body: InviteTokenRequest, request: Request, runtime: RuntimeDep
) -> InvitePreviewResponse:
    await rate_limits.enforce_rate_limit(
        runtime, rate_limits.INVITE_PREVIEW_PER_CLIENT, client_subject(request)
    )
    async with open_context(runtime) as ctx:
        preview = await invitations.preview_invite(ctx, body.token)
    invite, plan = preview.plan.invite, preview.plan.plan
    return InvitePreviewResponse(
        kind="plan",
        purpose="claim" if invite.purpose == "claim" else "join",
        requires_approval=invite.requires_approval,
        allow_guests=invite.allow_guests,
        placeholder_name=preview.plan.placeholder_name,
        expires_at=invite.expires_at,
        plan=PlanInvitePreview.model_validate(
            {
                "title": plan.title,
                "kind": plan.kind,
                "timing": timing_contract(plan_service.timing_of(plan)),
                "organizer_name": preview.plan.organizer_name,
            }
        ),
    )


@router.post("/v1/invites/redeem", response_model=RedeemInviteResponse, responses=PUBLIC_ERRORS)
async def redeem_invite(
    body: RedeemInviteRequest,
    request: Request,
    runtime: RuntimeDep,
    actor: OptionalActorDep,
) -> RedeemInviteResponse:
    """Join a plan. Without an account, invites that allow guests create a guest
    session (returned in ``session``)."""

    subject = client_subject(request)
    await rate_limits.enforce_rate_limit(runtime, rate_limits.INVITE_REDEEM_PER_CLIENT, subject)
    if actor is None:
        await rate_limits.enforce_rate_limit(
            runtime, rate_limits.GUEST_CREATION_PER_CLIENT, subject
        )
    async with open_context(runtime, actor) as ctx:
        redemption = await invitations.redeem_invite(
            ctx,
            body.token,
            display_name=body.display_name,
            merge_existing=body.merge_existing,
            device=device_info(body.device),
        )
    participant = redemption.plan.participant
    active = participant.access_state == AccessState.ACTIVE.value
    plan_view = plan_service.PlanView(plan=redemption.plan.plan, participant=participant)
    return RedeemInviteResponse(
        kind="plan",
        status="active" if active else "pending_approval",
        plan=plan_response(plan_view) if active else None,
        participant=participant_response(participant),
        session=token_response(redemption.plan.tokens) if redemption.plan.tokens else None,
    )
