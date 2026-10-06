"""Move a guest's plan participation to a registered account without new IDs.

* In-place upgrade (``target == guest``): rows stay linked to the same user and
  become ``identity_kind='user'``; the guest-only role becomes ``member``.
* Claim into an existing account: each row is relinked to the account. When the
  account already participates in that plan, the guest row is merged into the
  account's row (``merged_into_participant_id``) only after explicit consent;
  participant IDs and every historical reference stay unchanged.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from beluno.authorization.policy import AccessState, PlanRole
from beluno.contracts.errors import conflict
from beluno.db.models.plans import PlanParticipant
from beluno.modules.context import CommandContext
from beluno.modules.finance.merges import lock_plan_for_merge, transfer_merged_balances
from beluno.modules.plans.changes import bump, record_participant_change


async def transfer_guest_participations(
    ctx: CommandContext,
    guest_user_id: UUID,
    target_user_id: UUID,
    merge_existing: bool,
) -> None:
    if merge_existing and target_user_id != guest_user_id:
        # Merges move money: lock each affected plan (in one order) before any
        # participant row, as finance writers do.
        planned = await ctx.session.execute(
            select(PlanParticipant.plan_id).where(
                PlanParticipant.user_id == guest_user_id,
                PlanParticipant.access_state != AccessState.MERGED.value,
            )
        )
        merging = await _target_rows_by_plan(
            ctx, guest_user_id, target_user_id, [plan_id for (plan_id,) in planned.all()]
        )
        for plan_id in sorted(merging):
            await lock_plan_for_merge(ctx, plan_id)
    guest_rows = list(
        (
            await ctx.session.execute(
                select(PlanParticipant)
                .where(
                    PlanParticipant.user_id == guest_user_id,
                    PlanParticipant.access_state != AccessState.MERGED.value,
                )
                .order_by(PlanParticipant.id)
                .with_for_update()
            )
        ).scalars()
    )
    if target_user_id == guest_user_id:
        for row in guest_rows:
            row.identity_kind = "user"
            if row.role == PlanRole.GUEST.value:
                row.role = PlanRole.MEMBER.value
            bump(row, ctx)
            await ctx.session.flush()
            await record_participant_change(ctx, row, "plan_participant.guest_upgraded")
        return

    existing = await _target_rows_by_plan(
        ctx, guest_user_id, target_user_id, [row.plan_id for row in guest_rows]
    )
    conflicts = [row for row in guest_rows if row.plan_id in existing]
    if conflicts and not merge_existing:
        raise conflict(
            "PARTICIPANT_MERGE_REQUIRED",
            "This account already participates in plans joined as a guest",
            f"Confirm merging {len(conflicts)} guest participation(s) into this account",
        )
    for row in guest_rows:
        if row.plan_id in existing:
            row.access_state = AccessState.MERGED.value
            row.merged_into_participant_id = existing[row.plan_id]
            action = "plan_participant.merged"
        else:
            row.user_id = target_user_id
            row.identity_kind = "user"
            row.claimed_at = ctx.now
            if row.role == PlanRole.GUEST.value:
                row.role = PlanRole.MEMBER.value
            action = "plan_participant.claimed"
        bump(row, ctx)
        await ctx.session.flush()
        await record_participant_change(ctx, row, action)
        if row.access_state == AccessState.MERGED.value:
            await transfer_merged_balances(ctx, row.plan_id, row.id)


async def _target_rows_by_plan(
    ctx: CommandContext,
    guest_user_id: UUID,
    target_user_id: UUID,
    plan_ids: list[UUID],
) -> dict[UUID, UUID]:
    """Map plan → the target account's non-merged participant ID, read as the target."""

    if not plan_ids:
        return {}
    await ctx.act_as(target_user_id)
    rows = await ctx.session.execute(
        select(PlanParticipant.plan_id, PlanParticipant.id).where(
            PlanParticipant.user_id == target_user_id,
            PlanParticipant.plan_id.in_(plan_ids),
            PlanParticipant.access_state != AccessState.MERGED.value,
        )
    )
    mapping = {plan_id: participant_id for plan_id, participant_id in rows.all()}
    await ctx.act_as(guest_user_id)
    return mapping
