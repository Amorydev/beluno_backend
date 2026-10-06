"""Read models of the ledger: balances and status as of the last committed entry."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.db.models.finance import AccountBalance, LedgerAccount, LedgerHead
from beluno.modules.context import CommandContext


@dataclass(frozen=True)
class AccountView:
    account: LedgerAccount
    balance_minor: int


@dataclass(frozen=True)
class LedgerSnapshot:
    plan_id: UUID
    head: LedgerHead | None
    accounts: list[AccountView]


async def get_ledger(ctx: CommandContext, plan_id: UUID) -> LedgerSnapshot:
    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    return await ledger_snapshot(ctx, plan_id)


async def ledger_snapshot(ctx: CommandContext, plan_id: UUID) -> LedgerSnapshot:
    head = (
        await ctx.session.execute(
            select(LedgerHead)
            .where(LedgerHead.plan_id == plan_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    rows = await ctx.session.execute(
        select(LedgerAccount, AccountBalance.balance_minor)
        .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
        .where(LedgerAccount.plan_id == plan_id)
        .order_by(LedgerAccount.currency, LedgerAccount.participant_id.nulls_first())
        .execution_options(populate_existing=True)
    )
    return LedgerSnapshot(
        plan_id=plan_id,
        head=head,
        accounts=[AccountView(account, balance) for account, balance in rows.all()],
    )
