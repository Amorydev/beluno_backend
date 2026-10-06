"""Render finance read models as public contracts, and requests as domain drafts."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, TypeAdapter

from beluno.contracts.finance import (
    AdjustmentRequest,
    AdjustmentSplit,
    BaseAmountResponse,
    BudgetOverviewResponse,
    BudgetResponse,
    CommitmentCreateRequest,
    CommitmentResponse,
    CommitmentUpdateRequest,
    ConsolidationLineResponse,
    ConsolidationRateResponse,
    ConsolidationResponse,
    CurrencyResponse,
    EqualSplit,
    ExactSplit,
    ExpenseRequest,
    ExpenseResponse,
    ExplanationEntry,
    FundAvailabilityResponse,
    FundCountResponse,
    FundMovementRequest,
    FundMovementResponse,
    FundSettingsResponse,
    FundTarget,
    LedgerBalanceResponse,
    LedgerConfirmationResponse,
    LedgerResponse,
    PaidAmountResponse,
    PayerResponse,
    PercentageSplit,
    PostingResponse,
    RefundRequest,
    RefundResponse,
    RefundShareResponse,
    RevisionResponse,
    SettlementPreviewResponse,
    SettlementRequest,
    SettlementResponse,
    ShareResponse,
    SharesSplit,
    SpendResponse,
    Split,
    SuggestedFundPayout,
    SuggestedTransfer,
    TransactionResponse,
    WaiverRequest,
)
from beluno.db.models.finance import (
    Budget,
    Consolidation,
    CostCommitment,
    Currency,
    Expense,
    FundCount,
    FundMovement,
    FundSettings,
    FxSnapshot,
    Settlement,
)
from beluno.modules.context import CommandContext
from beluno.modules.finance.budgets import BudgetOverview, Spend
from beluno.modules.finance.commitments import CommitmentDraft, CommitmentView, commitment_view
from beluno.modules.finance.consolidation import ConsolidationView, consolidation_view
from beluno.modules.finance.expenses import (
    ExpenseDraft,
    ExpenseView,
    RefundDraft,
    RefundView,
    RevisionOrigin,
    RevisionView,
    expense_view,
)
from beluno.modules.finance.funds import AdjustmentDraft, MovementDraft
from beluno.modules.finance.fx import RateSource
from beluno.modules.finance.postings import FUND, Party
from beluno.modules.finance.rates import RateInput
from beluno.modules.finance.settlements import (
    SettlementDraft,
    SettlementView,
    WaiverDraft,
    settlement_view,
)
from beluno.modules.finance.splits import (
    Payer,
    Share,
    SplitEntry,
    SplitItem,
    SplitMethod,
    SplitSpec,
)
from beluno.modules.finance.states import CommitmentState, LedgerStatus
from beluno.modules.finance.views import (
    CurrencyPreview,
    ExplanationLine,
    LedgerSnapshot,
    TransactionView,
)

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
    if isinstance(split, AdjustmentSplit):
        return SplitSpec(
            SplitMethod.ADJUSTMENT,
            tuple(SplitEntry(s.participant_id, s.adjustment_minor) for s in split.shares),
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


def expense_draft(body: ExpenseRequest, origin: RevisionOrigin) -> ExpenseDraft:
    return ExpenseDraft(
        description=body.description,
        category=body.category,
        occurred_on=body.occurred_on,
        occurred_at=body.occurred_at,
        occurred_timezone=body.occurred_timezone,
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
        commitment_id=body.commitment_id,
        origin=origin,
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


def base_amount(
    currency: str, base_currency: str, amount_minor: int | None, rate: FxSnapshot | None
) -> BaseAmountResponse:
    if currency == base_currency:
        return BaseAmountResponse(
            currency=base_currency,
            amount_minor=amount_minor,
            rate=None,
            rate_source="identity",
            rate_as_of=None,
        )
    if rate is not None:
        return BaseAmountResponse.model_validate(
            {
                "currency": base_currency,
                "amount_minor": amount_minor,
                "rate": rate_text(rate.rate),
                "rate_source": rate.source,
                "rate_as_of": rate.as_of,
            }
        )
    return BaseAmountResponse(
        currency=base_currency, amount_minor=None, rate=None, rate_source=None, rate_as_of=None
    )


def is_personal(view: RevisionView) -> bool:
    """Its only payer is also its only sharer: spending that moves no balance."""

    return (
        len(view.payers) == 1
        and len(view.splits) == 1
        and view.payers[0].participant_id == view.splits[0].participant_id
    )


def revision_response(view: RevisionView) -> RevisionResponse:
    revision = view.revision
    base = base_amount(
        revision.currency, revision.base_currency, revision.base_amount_minor, view.base_rate
    )
    return RevisionResponse.model_validate(
        {
            "id": revision.id,
            "revision_number": revision.revision_number,
            "description": revision.description,
            "category": revision.category,
            "occurred_on": revision.occurred_on,
            "occurred_at": revision.occurred_at,
            "occurred_timezone": revision.occurred_timezone,
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
            "personal": is_personal(view),
            "base": base,
            "commitment_id": revision.commitment_id,
            "source": revision.source,
            "client_created_at": revision.client_created_at,
            "device_label": revision.device_label,
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
            "count_personal_spend": head.count_personal_spend if head else True,
            "settle_tolerance_minor": head.settle_tolerance_minor if head else 0,
            "confirmations": [
                LedgerConfirmationResponse(
                    participant_id=row.participant_id, confirmed_at=row.confirmed_at
                )
                for row in snapshot.confirmations
            ],
            "version": head.version if head else 0,
        }
    )


async def present_finance_current(ctx: CommandContext, entity: object) -> BaseModel | None:
    """The current snapshot of a finance row whose expected version was stale."""

    if isinstance(entity, Expense):
        return expense_response(await expense_view(ctx, entity))
    if isinstance(entity, Settlement):
        return settlement_response(await settlement_view(ctx, entity))
    if isinstance(entity, Budget):
        return budget_response(entity)
    if isinstance(entity, FundSettings):
        return fund_settings_response(entity)
    if isinstance(entity, CostCommitment):
        return commitment_response(await commitment_view(ctx, entity))
    if isinstance(entity, Consolidation):
        return consolidation_response(await consolidation_view(ctx, entity))
    return None


def consolidation_response(view: ConsolidationView) -> ConsolidationResponse:
    consolidation = view.consolidation
    return ConsolidationResponse.model_validate(
        {
            "id": consolidation.id,
            "plan_id": consolidation.plan_id,
            "base_currency": consolidation.base_currency,
            "state": consolidation.state,
            "rates": [
                ConsolidationRateResponse.model_validate(
                    {
                        "currency": rate.currency,
                        "rate": rate_text(snapshot.rate),
                        "source": snapshot.source,
                        "as_of": snapshot.as_of,
                    }
                )
                for rate, snapshot in view.rates
            ],
            "lines": [
                ConsolidationLineResponse(
                    participant_id=line.participant_id,
                    currency=line.currency,
                    amount_minor=line.amount_minor,
                    base_amount_minor=line.base_amount_minor,
                )
                for line in view.lines
            ],
            "created_by_user_id": consolidation.created_by_user_id,
            "created_at": consolidation.created_at,
            "reversed_by_user_id": consolidation.reversed_by_user_id,
            "reversed_at": consolidation.reversed_at,
            "version": consolidation.version,
            "updated_at": consolidation.updated_at,
        }
    )


# --- settlements and ledger views ---------------------------------------------------


def settlement_draft(body: SettlementRequest) -> SettlementDraft:
    return SettlementDraft(
        settlement_id=body.id,
        from_participant_id=body.from_participant_id,
        to_participant_id=body.to_participant_id,
        currency=body.currency,
        amount_minor=body.amount_minor,
        paid_currency=body.paid.currency if body.paid else None,
        paid_amount_minor=body.paid.amount_minor if body.paid else None,
        method=body.method,
        fee_minor=body.fee_minor,
        note=body.note,
        occurred_on=body.occurred_on,
    )


def waiver_draft(body: WaiverRequest) -> WaiverDraft:
    return WaiverDraft(
        settlement_id=body.id,
        debtor_participant_id=body.debtor_participant_id,
        creditor_participant_id=body.creditor_participant_id,
        currency=body.currency,
        amount_minor=body.amount_minor,
        note=body.note,
        occurred_on=body.occurred_on,
    )


def settlement_response(view: SettlementView) -> SettlementResponse:
    settlement = view.settlement
    paid = None
    if (
        settlement.paid_currency is not None
        and settlement.paid_amount_minor is not None
        and view.rate is not None
    ):
        paid = PaidAmountResponse(
            currency=settlement.paid_currency,
            amount_minor=settlement.paid_amount_minor,
            rate=rate_text(view.rate.rate),
        )
    return SettlementResponse.model_validate(
        {
            "id": settlement.id,
            "plan_id": settlement.plan_id,
            "kind": settlement.kind,
            "from_participant_id": settlement.from_participant_id,
            "to_participant_id": settlement.to_participant_id,
            "currency": settlement.currency,
            "amount_minor": settlement.amount_minor,
            "paid": paid,
            "method": settlement.method,
            "fee_minor": settlement.fee_minor,
            "note": settlement.note,
            "occurred_on": settlement.occurred_on,
            "status": settlement.status,
            "overpaid": settlement.overpaid,
            "recorded_by_user_id": settlement.recorded_by_user_id,
            "confirmed_by_user_id": settlement.confirmed_by_user_id,
            "confirmed_at": settlement.confirmed_at,
            "disputed_by_user_id": settlement.disputed_by_user_id,
            "disputed_at": settlement.disputed_at,
            "reversed_by_user_id": settlement.reversed_by_user_id,
            "reversed_at": settlement.reversed_at,
            "version": settlement.version,
            "created_at": settlement.created_at,
            "updated_at": settlement.updated_at,
        }
    )


def transaction_response(view: TransactionView) -> TransactionResponse:
    tx = view.transaction
    return TransactionResponse.model_validate(
        {
            "id": tx.id,
            "ledger_seq": tx.ledger_seq,
            "kind": tx.kind,
            "subtype": tx.subtype,
            "expense_id": tx.expense_id,
            "refund_id": tx.refund_id,
            "settlement_id": tx.settlement_id,
            "fund_movement_id": tx.fund_movement_id,
            "consolidation_id": tx.consolidation_id,
            "reverses_transaction_id": tx.reverses_transaction_id,
            "memo": tx.memo,
            "created_by_user_id": tx.created_by_user_id,
            "created_at": tx.created_at,
            "postings": [
                PostingResponse(
                    participant_id=posting.participant_id,
                    fund=posting.participant_id is None,
                    currency=posting.currency,
                    amount_minor=posting.amount_minor,
                )
                for posting in view.postings
            ],
        }
    )


def explanation_entry(line: ExplanationLine) -> ExplanationEntry:
    tx = line.transaction
    return ExplanationEntry.model_validate(
        {
            "transaction_id": tx.id,
            "ledger_seq": tx.ledger_seq,
            "kind": tx.kind,
            "subtype": tx.subtype,
            "expense_id": tx.expense_id,
            "settlement_id": tx.settlement_id,
            "fund_movement_id": tx.fund_movement_id,
            "description": line.description,
            "amount_minor": line.amount_minor,
            "balance_after_minor": line.balance_after_minor,
            "created_at": tx.created_at,
        }
    )


def preview_response(preview: CurrencyPreview) -> SettlementPreviewResponse:
    return SettlementPreviewResponse(
        currency=preview.currency,
        transfers=[
            SuggestedTransfer(
                from_participant_id=t.from_participant_id,
                to_participant_id=t.to_participant_id,
                amount_minor=t.amount_minor,
            )
            for t in preview.preview.transfers
        ],
        fund_payouts=[
            SuggestedFundPayout(to_participant_id=p.to_participant_id, amount_minor=p.amount_minor)
            for p in preview.preview.fund_payouts
        ],
    )


# --- budgets and commitments --------------------------------------------------------


def budget_response(budget: Budget) -> BudgetResponse:
    return BudgetResponse.model_validate(
        {
            "id": budget.id,
            "plan_id": budget.plan_id,
            "scope": budget.scope,
            "category": budget.category,
            "participant_id": budget.participant_id,
            "currency": budget.currency,
            "limit_minor": budget.limit_minor,
            "version": budget.version,
            "created_at": budget.created_at,
            "updated_at": budget.updated_at,
        }
    )


def spend_response(spend: Spend) -> SpendResponse:
    return SpendResponse(
        actual_minor=spend.actual,
        committed_minor=spend.committed,
        estimated_minor=spend.estimated,
        projected_minor=spend.projected,
    )


def budget_overview_response(overview: BudgetOverview) -> BudgetOverviewResponse:
    return BudgetOverviewResponse.model_validate(
        {
            "currency": overview.currency,
            "total": spend_response(overview.total),
            "categories": [
                {"category": category, "spend": spend_response(spend)}
                for category, spend in sorted(overview.categories.items())
            ],
            "unconverted": [
                {"tier": tier.value, "currency": currency, "amount_minor": amount}
                for (tier, currency), amount in sorted(overview.unconverted.items())
            ],
            "estimated_rates": overview.estimated_rates,
            "budgets": [
                {
                    "budget": budget_response(usage.budget),
                    "spend": spend_response(usage.spend),
                    "remaining_minor": usage.budget.limit_minor - usage.spend.projected,
                    "over_limit": usage.over_limit,
                    "days": [{"date": day, "actual_minor": amount} for day, amount in usage.days],
                }
                for usage in overview.budgets
            ],
        }
    )


def commitment_draft(body: CommitmentCreateRequest | CommitmentUpdateRequest) -> CommitmentDraft:
    return CommitmentDraft(
        category=body.category,
        description=body.description,
        currency=body.currency,
        amount_minor=body.amount_minor,
        state=CommitmentState(body.state),
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


def commitment_response(view: CommitmentView) -> CommitmentResponse:
    commitment = view.commitment
    return CommitmentResponse.model_validate(
        {
            "id": commitment.id,
            "plan_id": commitment.plan_id,
            "source_type": commitment.source_type,
            "source_id": commitment.source_id,
            "commitment_kind": commitment.commitment_kind,
            "state": commitment.state,
            "category": commitment.category,
            "description": commitment.description,
            "currency": commitment.currency,
            "amount_minor": commitment.amount_minor,
            "base": base_amount(
                commitment.currency, view.base_currency, commitment.base_amount_minor, view.rate
            ),
            "expense_id": commitment.expense_id,
            "created_by_user_id": commitment.created_by_user_id,
            "version": commitment.version,
            "created_at": commitment.created_at,
            "updated_at": commitment.updated_at,
        }
    )


# --- fund and adjustments -----------------------------------------------------------


def fund_settings_response(settings: FundSettings) -> FundSettingsResponse:
    return FundSettingsResponse(
        plan_id=settings.plan_id,
        custodian_participant_id=settings.custodian_participant_id,
        note=settings.note,
        target=(
            FundTarget(currency=settings.target_currency, amount_minor=settings.target_minor)
            if settings.target_currency is not None and settings.target_minor is not None
            else None
        ),
        version=settings.version,
        created_at=settings.created_at,
        updated_at=settings.updated_at,
    )


def fund_count_response(count: FundCount) -> FundCountResponse:
    return FundCountResponse(
        id=count.id,
        plan_id=count.plan_id,
        currency=count.currency,
        counted_minor=count.counted_minor,
        expected_minor=count.expected_minor,
        difference_minor=count.counted_minor - count.expected_minor,
        note=count.note,
        counted_by_user_id=count.counted_by_user_id,
        created_at=count.created_at,
    )


def fund_movement_response(movement: FundMovement) -> FundMovementResponse:
    return FundMovementResponse.model_validate(
        {
            "id": movement.id,
            "plan_id": movement.plan_id,
            "kind": movement.kind,
            "participant_id": movement.participant_id,
            "currency": movement.currency,
            "amount_minor": movement.amount_minor,
            "note": movement.note,
            "occurred_on": movement.occurred_on,
            "created_by_user_id": movement.created_by_user_id,
            "created_at": movement.created_at,
        }
    )


def movement_draft(body: FundMovementRequest) -> MovementDraft:
    return MovementDraft(
        movement_id=body.id,
        participant_id=body.participant_id,
        currency=body.currency,
        amount_minor=body.amount_minor,
        note=body.note,
        occurred_on=body.occurred_on,
    )


def adjustment_draft(body: AdjustmentRequest) -> AdjustmentDraft:
    return AdjustmentDraft(
        currency=body.currency,
        memo=body.memo,
        entries=tuple(
            (FUND if entry.fund else Party(entry.participant_id), entry.amount_minor)
            for entry in body.entries
        ),
    )
