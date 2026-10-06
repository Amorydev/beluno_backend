"""Finance side of merging a participant into another one.

Called inside placeholder and guest-account claims right after the row became
``merged``: every balance moves to the surviving participant as one explicit
``merge_transfer`` entry, so merged accounts stay at zero and later reversals
post to the survivor.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from beluno.db.ids import new_id
from beluno.modules.context import CommandContext
from beluno.modules.finance.ledger import LEDGER_ENTITY
from beluno.modules.sync_audit.recorder import ChangeScope, record_change

TRANSFER_SQL = text(
    "SELECT finance.transfer_merged_balances("
    ":plan_id, :participant_id, :transaction_id, :actor_user_id, :operation_id, :now)"
)


async def transfer_merged_balances(
    ctx: CommandContext, plan_id: UUID, participant_id: UUID
) -> None:
    actor = ctx.actor.user_id if ctx.actor else ctx.on_behalf_of
    version = (
        await ctx.session.execute(
            TRANSFER_SQL,
            {
                "plan_id": plan_id,
                "participant_id": participant_id,
                "transaction_id": new_id(),
                "actor_user_id": actor,
                "operation_id": ctx.operation_id,
                "now": ctx.now,
            },
        )
    ).scalar_one()
    if version is not None:
        await record_change(
            ctx,
            entity_type=LEDGER_ENTITY,
            entity_id=plan_id,
            entity_version=version,
            scope=ChangeScope.PLAN,
            scope_id=plan_id,
        )
