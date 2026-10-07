"""Plan invite links: join invites and placeholder claim invites.

Redemption locks the invite row, re-validates state/expiry/uses/email binding,
resolves or creates exactly one participant, then counts the use, all in one
transaction, so concurrent redemptions of the last use cannot both succeed and
retries never create duplicate participants.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select

from beluno.auth import AuthenticatedActor
from beluno.authorization.access import find_user_participant, load_plan, require_plan
from beluno.authorization.policy import (
    EDITABLE_PLAN_STATES,
    AccessState,
    PlanAction,
    PlanRole,
    PlanState,
    can_manage_participant,
)
from beluno.contracts.errors import (
    BelunoError,
    authentication_failed,
    conflict,
    feature_disabled,
    forbidden,
    invalid_state,
    invite_unavailable,
    not_found,
    validation_error,
)
from beluno.db.ids import new_id
from beluno.db.models.iam import User
from beluno.db.models.plans import Plan, PlanInvite, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.finance.merges import lock_plan_for_merge, transfer_merged_balances
from beluno.modules.iam.sessions import DeviceInfo, IssuedTokens, start_session
from beluno.modules.iam.users import create_guest_user
from beluno.modules.invite_links import (
    email_digest,
    expiry,
    issue_invite_token,
    unavailable_reason,
)
from beluno.modules.plans.changes import (
    bump,
    guest_linked,
    record_invite_change,
    record_participant_change,
)
from beluno.modules.plans.participants import (
    LIVE_STATES,
    build_participant,
    insert_participant,
    next_avatar_color,
    reactivate,
)


@dataclass(frozen=True)
class JoinInviteSettings:
    role: PlanRole
    allow_guests: bool
    requires_approval: bool
    intended_email: str | None
    max_uses: int | None
    expires_in_hours: int


@dataclass(frozen=True)
class CreatedInvite:
    invite: PlanInvite
    token: str


@dataclass(frozen=True)
class PlanInvitePreview:
    invite: PlanInvite
    plan: Plan
    organizer_name: str | None
    placeholder_name: str | None
    participant_count: int


@dataclass(frozen=True)
class PlanRedemption:
    plan: Plan
    participant: PlanParticipant
    tokens: IssuedTokens | None


async def create_join_invite(
    ctx: CommandContext,
    plan_id: UUID,
    settings: JoinInviteSettings,
) -> CreatedInvite:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_INVITES)
    assert access.role is not None
    if not can_manage_participant(access.role, PlanRole.MEMBER, new_role=settings.role):
        raise forbidden("Only the owner can invite administrators")
    hasher = ctx.runtime.require_hasher()
    return await _insert_invite(
        ctx,
        plan_id,
        purpose="join",
        target_participant_id=None,
        role=settings.role,
        allow_guests=settings.allow_guests,
        requires_approval=settings.requires_approval,
        intended_email_hash=(
            email_digest(hasher, settings.intended_email) if settings.intended_email else None
        ),
        max_uses=settings.max_uses,
        expires_in_hours=settings.expires_in_hours,
    )


async def create_claim_invite(
    ctx: CommandContext,
    plan_id: UUID,
    participant_id: UUID,
    expires_in_hours: int,
) -> CreatedInvite:
    """A single-use link that lets one person take over a placeholder participant."""

    if not ctx.settings.participant_claims_enabled:
        raise feature_disabled()
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_INVITES)
    placeholder = (
        await ctx.session.execute(
            select(PlanParticipant).where(
                PlanParticipant.plan_id == plan_id,
                PlanParticipant.id == participant_id,
                PlanParticipant.identity_kind == "placeholder",
                PlanParticipant.access_state == AccessState.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()
    if placeholder is None:
        raise not_found()
    return await _insert_invite(
        ctx,
        plan_id,
        purpose="claim",
        target_participant_id=placeholder.id,
        role=PlanRole.MEMBER,
        allow_guests=True,
        requires_approval=False,
        intended_email_hash=None,
        max_uses=1,
        expires_in_hours=expires_in_hours,
    )


async def list_invites(ctx: CommandContext, plan_id: UUID) -> list[PlanInvite]:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.MANAGE_INVITES)
    rows = await ctx.session.execute(
        select(PlanInvite).where(PlanInvite.plan_id == plan_id).order_by(PlanInvite.id.desc())
    )
    return list(rows.scalars())


async def revoke_invite(ctx: CommandContext, plan_id: UUID, invite_id: UUID) -> PlanInvite:
    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.MANAGE_INVITES)
    invite = await _managed_invite(ctx, plan_id, invite_id)
    await revoke_locked(ctx, invite)
    return invite


async def lock_live_invites(ctx: CommandContext, plan_ids: list[UUID]) -> list[PlanInvite]:
    """Lock the plans' active links; redemption waits on them, so nobody joins meanwhile."""

    if not plan_ids:
        return []
    rows = await ctx.session.execute(
        select(PlanInvite)
        .where(PlanInvite.plan_id.in_(plan_ids), PlanInvite.state == "active")
        .order_by(PlanInvite.id)
        .with_for_update()
    )
    return list(rows.scalars())


async def revoke_locked(ctx: CommandContext, invite: PlanInvite) -> None:
    """Revoke an invite the caller has locked and is allowed to manage."""

    if invite.state == "revoked":
        return
    invite.state = "revoked"
    invite.revoked_at = ctx.now
    bump(invite, ctx)
    await ctx.session.flush()
    await record_invite_change(ctx, invite, "plan_invite.revoked")


async def rotate_invite(ctx: CommandContext, plan_id: UUID, invite_id: UUID) -> CreatedInvite:
    """Revoke a link and issue a fresh one with the same settings and remaining lifetime."""

    current = await _managed_invite(ctx, plan_id, invite_id)
    if unavailable_reason(current, ctx.now) is not None:
        raise invalid_state("Only an active, unexpired invite with uses left can be rotated")
    old = await revoke_invite(ctx, plan_id, invite_id)
    remaining_hours = max(1, int((old.expires_at - ctx.now).total_seconds() // 3_600))
    return await _insert_invite(
        ctx,
        plan_id,
        purpose=old.purpose,
        target_participant_id=old.target_participant_id,
        role=PlanRole(old.role),
        allow_guests=old.allow_guests,
        requires_approval=old.requires_approval,
        intended_email_hash=old.intended_email_hash,
        max_uses=None if old.max_uses is None else max(1, old.max_uses - old.use_count),
        expires_in_hours=remaining_hours,
    )


async def find_by_digest(
    ctx: CommandContext,
    digest: bytes,
    *,
    for_update: bool,
) -> PlanInvite | None:
    """Requires the invite RLS context for ``digest`` to be set on the session."""

    statement = select(PlanInvite).where(PlanInvite.token_hash == digest)
    if for_update:
        statement = statement.with_for_update()
    return (await ctx.session.execute(statement)).scalar_one_or_none()


async def preview(ctx: CommandContext, invite: PlanInvite) -> PlanInvitePreview:
    plan = await ctx.session.get(Plan, invite.plan_id)
    if plan is None or not _accepts_new_people(plan):
        raise invite_unavailable()
    organizer = (
        await ctx.session.execute(
            select(PlanParticipant.display_name).where(
                PlanParticipant.plan_id == plan.id, PlanParticipant.role == PlanRole.OWNER.value
            )
        )
    ).scalar_one_or_none()
    placeholder_name = None
    if invite.target_participant_id is not None:
        placeholder_name = (
            await ctx.session.execute(
                select(PlanParticipant.display_name).where(
                    PlanParticipant.plan_id == plan.id,
                    PlanParticipant.id == invite.target_participant_id,
                )
            )
        ).scalar_one_or_none()
    participant_count = await ctx.session.scalar(
        select(func.count())
        .select_from(PlanParticipant)
        .where(
            PlanParticipant.plan_id == plan.id,
            PlanParticipant.access_state == AccessState.ACTIVE.value,
        )
    )
    return PlanInvitePreview(
        invite=invite,
        plan=plan,
        organizer_name=organizer,
        placeholder_name=placeholder_name,
        participant_count=participant_count or 0,
    )


async def redeem(
    ctx: CommandContext,
    invite: PlanInvite,
    *,
    display_name: str | None,
    merge_existing: bool,
    device: DeviceInfo,
    avatar_color: str | None = None,
) -> PlanRedemption:
    """``invite`` must be locked and already checked as usable by the caller."""

    # Not locked: redeemers are not participants yet, so the RLS update policy
    # would hide the row from FOR UPDATE. The locked invite row serializes redeems.
    plan = await ctx.session.get(Plan, invite.plan_id)
    if plan is None or not _accepts_new_people(plan):
        raise invite_unavailable()
    if invite.purpose == "claim" and not ctx.settings.participant_claims_enabled:
        raise feature_disabled()
    await _check_email_binding(ctx, invite)
    tokens = None
    if ctx.actor is None:
        tokens = await _start_guest(ctx, invite, display_name, device)
    if invite.purpose == "claim":
        participant = await _claim_placeholder(ctx, invite, merge_existing, avatar_color)
    else:
        existing = await find_user_participant(
            ctx, plan.id, ctx.require_actor().user_id, for_update=True
        )
        if existing is not None and existing.access_state in LIVE_STATES:
            # Retried or repeated redemption: same participant, no extra use counted.
            return PlanRedemption(plan=plan, participant=existing, tokens=tokens)
        participant = await _join(ctx, invite, existing, avatar_color)
    invite.use_count += 1
    bump(invite, ctx)
    await ctx.session.flush()
    await record_invite_change(ctx, invite, "plan_invite.redeemed")
    return PlanRedemption(plan=plan, participant=participant, tokens=tokens)


async def _join(
    ctx: CommandContext,
    invite: PlanInvite,
    existing: PlanParticipant | None,
    avatar_color: str | None,
) -> PlanParticipant:
    actor = ctx.require_actor()
    state = AccessState.PENDING_APPROVAL if invite.requires_approval else AccessState.ACTIVE
    role = PlanRole.GUEST if actor.is_guest else PlanRole(invite.role)
    if existing is not None:
        if existing.access_state == AccessState.REMOVED.value:
            raise forbidden("You were removed from this plan; ask an organizer to add you")
        reactivate(ctx, existing, role=role, access_state=state)
        existing.joined_via_invite_id = invite.id
        existing.avatar_color = avatar_color or existing.avatar_color
        await ctx.session.flush()
        await record_participant_change(ctx, existing, "plan_participant.rejoined_via_invite")
        return existing
    user = await ctx.session.get(User, actor.user_id)
    assert user is not None
    return await insert_participant(
        ctx,
        build_participant(
            ctx,
            plan_id=invite.plan_id,
            identity_kind="guest" if actor.is_guest else "user",
            user_id=user.id,
            display_name=user.display_name,
            role=role,
            avatar_color=avatar_color or await next_avatar_color(ctx, invite.plan_id),
            access_state=state,
            invite_id=invite.id,
        ),
        "plan_participant.joined_via_invite",
    )


async def _claim_placeholder(
    ctx: CommandContext,
    invite: PlanInvite,
    merge_existing: bool,
    avatar_color: str | None,
) -> PlanParticipant:
    actor = ctx.require_actor()
    if merge_existing and await find_user_participant(ctx, invite.plan_id, actor.user_id):
        # A merge moves money: serialize with finance writers before locking participants.
        await lock_plan_for_merge(ctx, invite.plan_id)
    placeholder = (
        await ctx.session.execute(
            select(PlanParticipant)
            .where(
                PlanParticipant.plan_id == invite.plan_id,
                PlanParticipant.id == invite.target_participant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        placeholder is None
        or placeholder.identity_kind != "placeholder"
        or placeholder.access_state != AccessState.ACTIVE.value
    ):
        raise invite_unavailable()
    existing = await find_user_participant(ctx, invite.plan_id, actor.user_id, for_update=True)
    if existing is not None:
        if not merge_existing:
            raise conflict(
                "PARTICIPANT_MERGE_REQUIRED",
                "You already participate in this plan",
                "Confirm merging the placeholder into your participation",
            )
        placeholder.access_state = AccessState.MERGED.value
        placeholder.merged_into_participant_id = existing.id
        bump(placeholder, ctx)
        await ctx.session.flush()
        # Members hear of it only when the person is still with them in the plan.
        linked = existing.access_state == AccessState.ACTIVE.value
        await record_participant_change(
            ctx,
            placeholder,
            "plan_participant.merged",
            activity=guest_linked(existing.id, placeholder.id) if linked else None,
        )
        await transfer_merged_balances(ctx, placeholder.plan_id, placeholder.id)
        return existing
    placeholder.user_id = actor.user_id
    placeholder.identity_kind = "guest" if actor.is_guest else "user"
    placeholder.claimed_at = ctx.now
    placeholder.avatar_color = avatar_color or placeholder.avatar_color
    if actor.is_guest:
        placeholder.role = PlanRole.GUEST.value
    bump(placeholder, ctx)
    await ctx.session.flush()
    await record_participant_change(ctx, placeholder, "plan_participant.claimed")
    return placeholder


async def _check_email_binding(ctx: CommandContext, invite: PlanInvite) -> None:
    if invite.intended_email_hash is None:
        return
    if ctx.actor is None or ctx.actor.is_guest:
        raise authentication_failed()
    user = await ctx.session.get(User, ctx.actor.user_id)
    hasher = ctx.runtime.require_hasher()
    if (
        user is None
        or user.email is None
        or email_digest(hasher, user.email) != invite.intended_email_hash
    ):
        raise BelunoError(
            status=403,
            code="INVITE_EMAIL_MISMATCH",
            title="This invite was sent to a different email address",
        )


async def _start_guest(
    ctx: CommandContext,
    invite: PlanInvite,
    display_name: str | None,
    device: DeviceInfo,
) -> IssuedTokens:
    if not invite.allow_guests:
        raise authentication_failed()
    if not ctx.settings.guest_access_enabled:
        raise feature_disabled()
    if display_name is None:
        raise validation_error("display_name is required to join as a guest")
    guest = await create_guest_user(ctx, display_name)
    tokens = await start_session(ctx, guest, auth_method="guest_invite", device=device)
    ctx.actor = AuthenticatedActor(
        user_id=guest.id,
        session_id=tokens.session_id,
        authenticated_at=ctx.now,
        is_guest=True,
    )
    return tokens


async def _insert_invite(
    ctx: CommandContext,
    plan_id: UUID,
    *,
    purpose: str,
    target_participant_id: UUID | None,
    role: PlanRole,
    allow_guests: bool,
    requires_approval: bool,
    intended_email_hash: bytes | None,
    max_uses: int | None,
    expires_in_hours: int,
) -> CreatedInvite:
    token = issue_invite_token(ctx.runtime.require_hasher())
    invite = PlanInvite(
        id=new_id(),
        plan_id=plan_id,
        purpose=purpose,
        target_participant_id=target_participant_id,
        token_hash=token.digest,
        role=role.value,
        allow_guests=allow_guests,
        requires_approval=requires_approval,
        intended_email_hash=intended_email_hash,
        max_uses=max_uses,
        use_count=0,
        expires_at=expiry(ctx.now, expires_in_hours),
        state="active",
        revoked_at=None,
        created_by_user_id=ctx.require_actor().user_id,
        version=1,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    ctx.session.add(invite)
    await ctx.session.flush()
    await record_invite_change(ctx, invite, "plan_invite.created", {"role": role.value})
    return CreatedInvite(invite=invite, token=token.raw)


def _accepts_new_people(plan: Plan) -> bool:
    """Closed or deleting plans are read-only, so their links stop admitting anyone."""

    return plan.deletion_scheduled_at is None and PlanState(plan.state) in EDITABLE_PLAN_STATES


async def _managed_invite(ctx: CommandContext, plan_id: UUID, invite_id: UUID) -> PlanInvite:
    invite = (
        await ctx.session.execute(
            select(PlanInvite)
            .where(PlanInvite.plan_id == plan_id, PlanInvite.id == invite_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise not_found()
    return invite
