"""Deleting an account: the person becomes "Former member" everywhere, at once.

One transaction, after a recent sign-in:

1. Refuse while the person owns a plan someone else is still active in
   (``409 OWNER_TRANSFER_REQUIRED``); plans they own alone are scheduled for
   deletion.
2. Every participant row they ever had is renamed "Former member"; rows that
   were active (or waiting for approval) become ``left``.
3. Their crews are deleted, and they are removed from other people's crews.
4. Identities and email challenges go, the profile is scrubbed (no email,
   locale, zone, or currency; status ``deleted``), and every session is revoked.

Money history never changes: postings, revisions, and settlements stay, and the
others can still settle with a participant who left.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, text

from beluno.authorization.access import load_plan
from beluno.authorization.policy import AccessState, PlanRole
from beluno.contracts.errors import conflict, step_up_required
from beluno.db.models.people import Crew
from beluno.db.models.plans import Plan, PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.iam import users
from beluno.modules.iam.sessions import revoke_all_sessions
from beluno.modules.people import crews
from beluno.modules.plans.changes import bump, record_participant_change, record_plan_change
from beluno.modules.sync_audit.recorder import ChangeScope, record_change

FORMER_MEMBER = "Former member"
DELETED = "deleted"
LIVE_STATES = (AccessState.ACTIVE.value, AccessState.PENDING_APPROVAL.value)
FORGET_IN_CREWS = text(
    "SELECT crew_id, owner_user_id, version, removed FROM people.forget_member()"
)
FORGET_CREDENTIALS = text("SELECT iam.forget_actor_credentials()")


async def delete_account(ctx: CommandContext) -> None:
    actor = ctx.require_actor()
    if not ctx.step_up_is_fresh:
        raise step_up_required()
    user = await users.load_user(ctx, actor.user_id, for_update=True)
    rows = list(
        (
            await ctx.session.execute(
                select(PlanParticipant)
                .where(PlanParticipant.user_id == user.id)
                .order_by(PlanParticipant.plan_id, PlanParticipant.id)
            )
        ).scalars()
    )
    owned = [
        row
        for row in rows
        if row.role == PlanRole.OWNER.value and row.access_state == AccessState.ACTIVE.value
    ]
    # Lock each owned plan before looking at who else is in it, so nobody joins in between.
    plans = [(await load_plan(ctx, row.plan_id, for_update=True)).plan for row in owned]
    for row in owned:
        if await _others_active(ctx, row):
            raise conflict(
                "OWNER_TRANSFER_REQUIRED",
                "Transfer ownership before deleting your account",
                "Make someone else the owner of every plan others are still in",
            )
    for plan in plans:
        await _schedule_plan_deletion(ctx, plan)
    for row in rows:
        await _forget_participant(ctx, row)
    await _forget_crews(ctx, user.id)
    await ctx.session.execute(FORGET_CREDENTIALS)
    await revoke_all_sessions(ctx, user.id, reason="account_deleted")
    await users.scrub_profile(ctx, user, display_name=FORMER_MEMBER, status=DELETED)


async def _others_active(ctx: CommandContext, own: PlanParticipant) -> bool:
    count = await ctx.session.execute(
        select(func.count())
        .select_from(PlanParticipant)
        .where(
            PlanParticipant.plan_id == own.plan_id,
            PlanParticipant.id != own.id,
            PlanParticipant.access_state == AccessState.ACTIVE.value,
        )
    )
    return count.scalar_one() > 0


async def _schedule_plan_deletion(ctx: CommandContext, plan: Plan) -> None:
    """A plan nobody else is in goes with its owner (restorable until purged)."""

    if plan.deletion_scheduled_at is not None:
        return
    plan.deletion_scheduled_at = ctx.now
    bump(plan, ctx)
    await ctx.session.flush()
    await record_plan_change(ctx, plan, "plan.deletion_scheduled", {"reason": "account_deleted"})


async def _forget_participant(ctx: CommandContext, row: PlanParticipant) -> None:
    # An owner stays an owner (only plans they own alone get here, scheduled for deletion).
    leaving = row.access_state in LIVE_STATES and row.role != PlanRole.OWNER.value
    row.display_name = FORMER_MEMBER
    if leaving:
        row.access_state = AccessState.LEFT.value
        row.left_at = ctx.now
        row.capabilities = []
    bump(row, ctx)
    await ctx.session.flush()
    action = "plan_participant.left" if leaving else "plan_participant.renamed"
    await record_participant_change(ctx, row, action, {"reason": "account_deleted"})


async def _forget_crews(ctx: CommandContext, user_id: UUID) -> None:
    own = await ctx.session.execute(
        select(Crew.id).where(Crew.owner_user_id == user_id, Crew.deleted_at.is_(None))
    )
    for crew_id in list(own.scalars()):
        await crews.delete_crew(ctx, crew_id)
    for crew_id, owner_id, version, removed in (await ctx.session.execute(FORGET_IN_CREWS)).all():
        # Their owners' devices learn the crew changed (or is gone).
        await record_change(
            ctx,
            entity_type=crews.CREW_ENTITY,
            entity_id=crew_id,
            entity_version=version,
            scope=ChangeScope.USER,
            scope_id=owner_id,
            operation="delete" if removed else "upsert",
        )
