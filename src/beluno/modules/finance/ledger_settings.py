"""A plan's money settings and the participants' confirmations of its ledger.

Settings live on the ledger head so finance keeps owning money rules:

* ``count_personal_spend``: whether the budget view counts personal expenses
  (the only payer is the only sharer);
* ``settle_tolerance_minor``: base-currency balances at or under it count as
  settled for the ledger status and are left out of suggested transfers.
  Postings always stay exact.

A confirmation says the ledger looked right to one participant at one
``ledger_seq``. Any later entry makes it stale; it never blocks anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.contracts.errors import conflict, invalid_state
from beluno.db.models.finance import LedgerConfirmation, LedgerHead
from beluno.modules.context import CommandContext
from beluno.modules.finance.errors import amount_out_of_range
from beluno.modules.finance.ledger import LEDGER_ENTITY, ledger_for, open_ledger
from beluno.modules.finance.views import LedgerSnapshot, ledger_snapshot
from beluno.modules.sync_audit.recorder import record_audit

# 1 % of the largest amount an entry may carry.
MAX_SETTLE_TOLERANCE = 10**10


@dataclass(frozen=True)
class LedgerSettingsDraft:
    count_personal_spend: bool | None = None
    settle_tolerance_minor: int | None = None


async def configure_ledger(
    ctx: CommandContext, plan_id: UUID, draft: LedgerSettingsDraft
) -> LedgerSnapshot:
    """Change the money settings that were sent; the ledger status follows the tolerance."""

    ledger = await open_ledger(ctx, plan_id, PlanAction.CONFIGURE_LEDGER)
    head = ledger.head
    if draft.count_personal_spend is not None:
        head.count_personal_spend = draft.count_personal_spend
    if draft.settle_tolerance_minor is not None:
        if not 0 <= draft.settle_tolerance_minor <= MAX_SETTLE_TOLERANCE:
            raise amount_out_of_range(
                f"settle_tolerance_minor must be between 0 and {MAX_SETTLE_TOLERANCE}"
            )
        head.settle_tolerance_minor = draft.settle_tolerance_minor
    await ledger.update_status()
    await ledger.touch()
    await record_audit(
        ctx,
        action="finance.ledger_configured",
        entity_type=LEDGER_ENTITY,
        entity_id=plan_id,
        plan_id=plan_id,
        metadata={
            "count_personal_spend": head.count_personal_spend,
            "settle_tolerance_minor": head.settle_tolerance_minor,
        },
    )
    return await ledger_snapshot(ctx, plan_id)


async def confirm_ledger(ctx: CommandContext, plan_id: UUID, ledger_seq: int) -> LedgerSnapshot:
    """The caller says the ledger as of ``ledger_seq`` looks right (an intent command)."""

    access = await load_plan(ctx, plan_id, for_update=True)
    require_plan(access, PlanAction.CONFIRM_LEDGER)
    # Confirming never creates a ledger: with no entries there is nothing to confirm.
    if await ctx.session.get(LedgerHead, plan_id) is None:
        raise invalid_state("There are no entries to confirm yet")
    ledger = await ledger_for(ctx, access)
    own = ledger.access.participant
    assert own is not None
    if ledger.head.ledger_seq == 0:
        raise invalid_state("There are no entries to confirm yet")
    if ledger_seq != ledger.head.ledger_seq:
        raise conflict(
            "LEDGER_CHANGED",
            "The ledger changed since you looked",
            "Review the latest entries and confirm again",
        )
    key = (plan_id, own.id, ledger_seq)
    if await ctx.session.get(LedgerConfirmation, key) is None:
        ctx.session.add(
            LedgerConfirmation(
                plan_id=plan_id,
                participant_id=own.id,
                ledger_seq=ledger_seq,
                confirmed_by_user_id=ctx.require_actor().user_id,
                confirmed_at=ctx.now,
            )
        )
        await ctx.session.flush()
        await ledger.touch()
        await record_audit(
            ctx,
            action="finance.ledger_confirmed",
            entity_type=LEDGER_ENTITY,
            entity_id=plan_id,
            plan_id=plan_id,
            metadata={"ledger_seq": ledger_seq},
        )
    return await ledger_snapshot(ctx, plan_id)
