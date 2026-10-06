"""Render finance read models as public contracts, and requests as domain drafts."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, TypeAdapter

from beluno.contracts.finance import (
    BaseAmountResponse,
    CurrencyResponse,
    EqualSplit,
    ExactSplit,
    ExpenseRequest,
    ExpenseResponse,
    FundAvailabilityResponse,
    LedgerBalanceResponse,
    LedgerResponse,
    PayerResponse,
    PercentageSplit,
    RefundRequest,
    RefundResponse,
    RefundShareResponse,
    RevisionResponse,
    ShareResponse,
    SharesSplit,
    Split,
)
from beluno.db.models.finance import Currency, Expense
from beluno.modules.context import CommandContext
from beluno.modules.finance.expenses import (
    ExpenseDraft,
    ExpenseView,
    RateInput,
    RefundDraft,
    RefundView,
    RevisionView,
    expense_view,
)
from beluno.modules.finance.fx import RateSource
from beluno.modules.finance.postings import FUND, Party
from beluno.modules.finance.splits import (
    Payer,
    Share,
    SplitEntry,
    SplitItem,
    SplitMethod,
    SplitSpec,
)
from beluno.modules.finance.states import LedgerStatus
from beluno.modules.finance.views import LedgerSnapshot

SPLIT_ADAPTER: TypeAdapter[Split] = TypeAdapter(Split)


def rate_text(rate: Decimal) -> str:
    return format(rate.normalize(), "f")


def currency_response(currency: Currency) -> CurrencyResponse:
    return CurrencyResponse.model_validate(
        {"code": currency.code, "exponent": currency.exponent, "state": currency.state}
    )


# --- requests -> drafts ------------------------------------------------------------


def split_spec(split: Split) -> SplitSpec:
    if isinstance(split, EqualSplit):
        return SplitSpec(SplitMethod.EQUAL, tuple(SplitEntry(pid) for pid in split.participant_ids))
    if isinstance(split, ExactSplit):
        return SplitSpec(
            SplitMethod.EXACT,
            tuple(SplitEntry(s.participant_id, s.amount_minor) for s in split.shares),
        )
    if isinstance(split, PercentageSplit):
        return SplitSpec(
            SplitMethod.PERCENTAGE,
            tuple(SplitEntry(s.participant_id, s.basis_points) for s in split.shares),
        )
    if isinstance(split, SharesSplit):
        return SplitSpec(
            SplitMethod.SHARES,
            tuple(SplitEntry(s.participant_id, s.weight) for s in split.shares),
        )
    return SplitSpec(
        SplitMethod.ITEMIZED,
        items=tuple(
            SplitItem(
                item.amount_minor,
                tuple(item.participant_ids),
                tuple(item.weights) if item.weights is not None else None,
            )
            for item in split.items
        ),
        extras=tuple(extra.amount_minor for extra in split.extras),
    )


def expense_draft(body: ExpenseRequest) -> ExpenseDraft:
    return ExpenseDraft(
        description=body.description,
        category=body.category,
        occurred_on=body.occurred_on,
        notes=body.notes,
        amount_minor=body.amount_minor,
        currency=body.currency,
        payers=tuple(
            Payer(None if payer.fund else payer.participant_id, payer.amount_minor)
            for payer in body.payers
        ),
        split=split_spec(body.split),
        split_input=SPLIT_ADAPTER.dump_python(body.split, mode="json"),
        base_rate=(
            RateInput(
                rate=body.base_rate.rate,
                source=RateSource(body.base_rate.source),
                as_of=body.base_rate.as_of,
            )
            if body.base_rate
            else None
        ),
    )


def refund_draft(body: RefundRequest) -> RefundDraft:
    recipient = FUND if body.recipient.fund else Party(body.recipient.participant_id)
    return RefundDraft(
        refund_id=body.id,
        amount_minor=body.amount_minor,
        recipient=recipient,
        shares=(
            tuple(Share(share.participant_id, share.amount_minor) for share in body.shares)
            if body.shares is not None
            else None
        ),
        note=body.note,
    )


# --- read models -> responses ------------------------------------------------------


def revision_response(view: RevisionView) -> RevisionResponse:
    revision = view.revision
    if revision.currency == revision.base_currency:
        base = BaseAmountResponse(
            currency=revision.base_currency,
            amount_minor=revision.base_amount_minor,
            rate=None,
            rate_source="identity",
            rate_as_of=None,
        )
    elif view.base_rate is not None:
        base = BaseAmountResponse.model_validate(
            {
                "currency": revision.base_currency,
                "amount_minor": revision.base_amount_minor,
                "rate": rate_text(view.base_rate.rate),
                "rate_source": view.base_rate.source,
                "rate_as_of": view.base_rate.as_of,
            }
        )
    else:
        base = BaseAmountResponse(
            currency=revision.base_currency,
            amount_minor=None,
            rate=None,
            rate_source=None,
            rate_as_of=None,
        )
    return RevisionResponse.model_validate(
        {
            "id": revision.id,
            "revision_number": revision.revision_number,
            "description": revision.description,
            "category": revision.category,
            "occurred_on": revision.occurred_on,
            "notes": revision.notes,
            "amount_minor": revision.amount_minor,
            "currency": revision.currency,
            "payers": [
                PayerResponse(
                    participant_id=payer.participant_id,
                    fund=payer.participant_id is None,
                    amount_minor=payer.amount_minor,
                )
                for payer in view.payers
            ],
            "split": revision.split_input,
            "split_algorithm": revision.split_algorithm,
            "shares": [
                ShareResponse(participant_id=split.participant_id, owed_minor=split.owed_minor)
                for split in view.splits
            ],
            "base": base,
            "created_by_user_id": revision.created_by_user_id,
            "created_at": revision.created_at,
        }
    )


def refund_response(view: RefundView) -> RefundResponse:
    refund = view.refund
    return RefundResponse(
        id=refund.id,
        amount_minor=refund.amount_minor,
        currency=refund.currency,
        recipient_participant_id=refund.recipient_participant_id,
        fund=refund.recipient_participant_id is None,
        shares=[
            RefundShareResponse(
                participant_id=share.participant_id, amount_minor=share.amount_minor
            )
            for share in view.shares
        ],
        note=refund.note,
        reversed=view.reversed,
        created_by_user_id=refund.created_by_user_id,
        created_at=refund.created_at,
    )


def expense_response(view: ExpenseView) -> ExpenseResponse:
    expense = view.expense
    return ExpenseResponse.model_validate(
        {
            "id": expense.id,
            "plan_id": expense.plan_id,
            "state": expense.state,
            "revision": revision_response(view.current),
            "refunds": [refund_response(refund) for refund in view.refunds],
            "refunded_minor": view.refunded_minor,
            "created_by_user_id": expense.created_by_user_id,
            "version": expense.version,
            "created_at": expense.created_at,
            "updated_at": expense.updated_at,
            "voided_at": expense.voided_at,
        }
    )


def ledger_response(snapshot: LedgerSnapshot) -> LedgerResponse:
    head = snapshot.head
    return LedgerResponse.model_validate(
        {
            "plan_id": snapshot.plan_id,
            "status": head.status if head else LedgerStatus.OPEN.value,
            "ledger_seq": head.ledger_seq if head else 0,
            "disputed_settlements": head.disputed_settlements if head else 0,
            "balances": [
                LedgerBalanceResponse(
                    participant_id=view.account.participant_id,
                    fund=view.account.participant_id is None,
                    currency=view.account.currency,
                    balance_minor=view.balance_minor,
                )
                for view in snapshot.accounts
            ],
            "fund": [
                FundAvailabilityResponse(
                    currency=view.account.currency, available_minor=-view.balance_minor
                )
                for view in snapshot.accounts
                if view.account.participant_id is None
            ],
            "version": head.version if head else 0,
        }
    )


async def present_finance_current(ctx: CommandContext, entity: object) -> BaseModel | None:
    """The current snapshot of a finance row whose expected version was stale."""

    if isinstance(entity, Expense):
        return expense_response(await expense_view(ctx, entity))
    return None
