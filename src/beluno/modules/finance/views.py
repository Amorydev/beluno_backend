"""Read models of the ledger: balances and status as of the last committed entry."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select

from beluno.authorization.access import load_plan, require_plan
from beluno.authorization.policy import PlanAction
from beluno.db.models.finance import (
    AccountBalance,
    Expense,
    ExpenseRevision,
    LedgerAccount,
    LedgerHead,
    LedgerPosting,
    LedgerTransaction,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.preview import SettlementPreview, simplify_debts


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


@dataclass(frozen=True)
class PostingView:
    participant_id: UUID | None
    currency: str
    amount_minor: int


@dataclass(frozen=True)
class TransactionView:
    transaction: LedgerTransaction
    postings: list[PostingView]


@dataclass(frozen=True)
class ExplanationLine:
    """One entry that moved a participant's (or the fund's) balance in one currency."""

    transaction: LedgerTransaction
    amount_minor: int
    balance_after_minor: int
    description: str | None


async def list_transactions(
    ctx: CommandContext, plan_id: UUID, *, after_seq: int | None, limit: int
) -> list[TransactionView]:
    """The plan's journal in ledger order; every entry balances per currency."""

    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    statement = select(LedgerTransaction).where(LedgerTransaction.plan_id == plan_id)
    if after_seq is not None:
        statement = statement.where(LedgerTransaction.ledger_seq > after_seq)
    transactions = list(
        (
            await ctx.session.execute(statement.order_by(LedgerTransaction.ledger_seq).limit(limit))
        ).scalars()
    )
    postings: dict[UUID, list[PostingView]] = {tx.id: [] for tx in transactions}
    if transactions:
        rows = await ctx.session.execute(
            select(
                LedgerPosting.transaction_id,
                LedgerAccount.participant_id,
                LedgerPosting.currency,
                LedgerPosting.amount_minor,
            )
            .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
            .where(LedgerPosting.transaction_id.in_(postings))
            .order_by(LedgerPosting.currency, LedgerAccount.participant_id.nulls_first())
        )
        for transaction_id, participant_id, currency, amount in rows.all():
            postings[transaction_id].append(PostingView(participant_id, currency, amount))
    return [TransactionView(tx, postings[tx.id]) for tx in transactions]


async def explain_balance(
    ctx: CommandContext,
    plan_id: UUID,
    *,
    participant_id: UUID | None,
    currency: str,
    after_seq: int | None,
    limit: int,
) -> list[ExplanationLine]:
    """Every entry behind one balance, with the running balance after each."""

    access = await load_plan(ctx, plan_id)
    require_plan(access, PlanAction.VIEW_FINANCE)
    account_filter = (
        LedgerAccount.participant_id.is_(None)
        if participant_id is None
        else LedgerAccount.participant_id == participant_id
    )
    account = (
        await ctx.session.execute(
            select(LedgerAccount.id).where(
                LedgerAccount.plan_id == plan_id, LedgerAccount.currency == currency, account_filter
            )
        )
    ).scalar_one_or_none()
    if account is None:
        return []
    running = (
        select(
            LedgerPosting.transaction_id,
            LedgerPosting.amount_minor,
            func.sum(LedgerPosting.amount_minor)
            .over(order_by=LedgerTransaction.ledger_seq)
            .label("balance_after"),
            LedgerTransaction.ledger_seq,
        )
        .join(LedgerTransaction, LedgerTransaction.id == LedgerPosting.transaction_id)
        .where(LedgerPosting.account_id == account)
        .subquery()
    )
    statement = (
        select(LedgerTransaction, running.c.amount_minor, running.c.balance_after)
        .join(running, running.c.transaction_id == LedgerTransaction.id)
        .order_by(running.c.ledger_seq)
        .limit(limit)
    )
    if after_seq is not None:
        statement = statement.where(running.c.ledger_seq > after_seq)
    rows = (await ctx.session.execute(statement)).all()
    revisions = [tx.revision_id for tx, _, _ in rows if tx.revision_id is not None]
    descriptions: dict[UUID, str] = {}
    if revisions:
        found = await ctx.session.execute(
            select(ExpenseRevision.id, ExpenseRevision.description).where(
                ExpenseRevision.id.in_(revisions)
            )
        )
        descriptions = dict(found.all())
    expense_titles = await _current_descriptions(ctx, [tx for tx, _, _ in rows])
    return [
        ExplanationLine(
            transaction=tx,
            amount_minor=amount,
            balance_after_minor=int(balance_after),
            description=(descriptions.get(tx.revision_id) if tx.revision_id else None)
            or (expense_titles.get(tx.expense_id) if tx.expense_id else None),
        )
        for tx, amount, balance_after in rows
    ]


async def _current_descriptions(
    ctx: CommandContext, transactions: list[LedgerTransaction]
) -> dict[UUID, str]:
    """Reversals and refunds are explained by their expense's current description."""

    expense_ids = {tx.expense_id for tx in transactions if tx.expense_id is not None}
    if not expense_ids:
        return {}
    rows = await ctx.session.execute(
        select(Expense.id, ExpenseRevision.description)
        .join(ExpenseRevision, ExpenseRevision.id == Expense.current_revision_id)
        .where(Expense.id.in_(expense_ids))
    )
    return dict(rows.all())


@dataclass(frozen=True)
class CurrencyPreview:
    currency: str
    preview: SettlementPreview


async def settlement_preview(
    ctx: CommandContext, plan_id: UUID, *, currency: str | None
) -> list[CurrencyPreview]:
    """Suggested transfers per currency; nothing is posted until someone records them."""

    snapshot = await get_ledger(ctx, plan_id)
    currencies = sorted({view.account.currency for view in snapshot.accounts})
    if currency is not None:
        currencies = [code for code in currencies if code == currency]
    previews = []
    for code in currencies:
        balances = {
            view.account.participant_id: view.balance_minor
            for view in snapshot.accounts
            if view.account.currency == code and view.account.participant_id is not None
        }
        fund = sum(
            -view.balance_minor
            for view in snapshot.accounts
            if view.account.currency == code and view.account.participant_id is None
        )
        previews.append(CurrencyPreview(code, simplify_debts(balances, fund_available=fund)))
    return previews
