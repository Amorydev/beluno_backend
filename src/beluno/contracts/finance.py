"""Financial ledger contracts: currencies, expenses, refunds, and the ledger view.

Amounts are integers in minor units of the named currency (``amount_minor``);
the currency's exponent comes from ``GET /v1/currencies``. Exchange rates travel
as decimal strings so no client ever parses them as floating point.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from beluno.contracts.common import CurrencyCode, Label, LongText, TimezoneName, clean_text

ExpenseCategory = Literal[
    "food", "lodging", "transport", "activities", "shopping", "groceries", "fees", "other"
]
Description = Annotated[
    str,
    StringConstraints(max_length=400),
    AfterValidator(clean_text),
    StringConstraints(min_length=1, max_length=200),
]
Note = Annotated[str, AfterValidator(clean_text), StringConstraints(max_length=500)]
RateText = Annotated[str, StringConstraints(pattern=r"^\d{1,10}(\.\d{1,12})?$")]
ParticipantIds = Annotated[list[UUID], Field(min_length=1, max_length=100)]


class CurrencyResponse(BaseModel):
    code: str
    exponent: int
    state: Literal["supported", "retired"]


class RateRequest(BaseModel):
    """Quote-currency units for one unit of the expense currency (major units)."""

    model_config = ConfigDict(extra="forbid")

    rate: RateText
    source: Literal["manual", "estimated"] = "manual"
    as_of: AwareDatetime | None = None


class PayerRequest(BaseModel):
    """A participant who paid, or the plan fund (``fund: true``)."""

    model_config = ConfigDict(extra="forbid")

    participant_id: UUID | None = None
    fund: bool = False
    amount_minor: StrictInt

    @model_validator(mode="after")
    def one_account(self) -> Self:
        if self.fund == (self.participant_id is not None):
            raise ValueError("name a participant_id or set fund to true")
        return self


class ExactShare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID
    amount_minor: StrictInt


class PercentageShare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID
    basis_points: StrictInt = Field(description="Hundredths of a percent; all shares total 10000")


class WeightShare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID
    weight: StrictInt


class SplitAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID
    adjustment_minor: StrictInt = Field(
        description="Added to (or, when negative, taken off) this person's equal share"
    )


class EqualSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["equal"]
    participant_ids: ParticipantIds


class ExactSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["exact"]
    shares: Annotated[list[ExactShare], Field(min_length=1, max_length=100)]


class PercentageSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["percentage"]
    shares: Annotated[list[PercentageShare], Field(min_length=1, max_length=100)]


class SharesSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["shares"]
    shares: Annotated[list[WeightShare], Field(min_length=1, max_length=100)]


class SplitItem(BaseModel):
    """One bill line, split equally among its participants or by ``weights``."""

    model_config = ConfigDict(extra="forbid")

    label: Label | None = None
    amount_minor: StrictInt
    participant_ids: ParticipantIds
    weights: Annotated[list[StrictInt], Field(min_length=1, max_length=100)] | None = None


class SplitExtra(BaseModel):
    """Tax, tip, or a service charge, spread over the item subtotals."""

    model_config = ConfigDict(extra="forbid")

    label: Label | None = None
    amount_minor: StrictInt


class ItemizedSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["itemized"]
    items: Annotated[list[SplitItem], Field(min_length=1, max_length=100)]
    extras: Annotated[list[SplitExtra], Field(max_length=10)] = Field(default_factory=list)


class AdjustmentSplit(BaseModel):
    """Equal shares after per-person adjustments; nobody may owe less than zero."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["adjustment"]
    shares: Annotated[list[SplitAdjustment], Field(min_length=1, max_length=100)]


Split = Annotated[
    EqualSplit | ExactSplit | PercentageSplit | SharesSplit | ItemizedSplit | AdjustmentSplit,
    Field(discriminator="method"),
]


class ExpenseRequest(BaseModel):
    """A full expense value: creating it or replacing it appends a revision."""

    model_config = ConfigDict(extra="forbid")

    description: Description
    category: ExpenseCategory = "other"
    occurred_on: date
    occurred_at: AwareDatetime | None = Field(
        default=None, description="When it happened; occurred_on must be its local date"
    )
    occurred_timezone: TimezoneName | None = Field(
        default=None, description="IANA zone of occurred_at (shown as 20:10 JST)"
    )
    notes: LongText | None = None
    amount_minor: StrictInt
    currency: CurrencyCode
    payers: Annotated[list[PayerRequest], Field(min_length=1, max_length=100)]
    split: Split
    base_rate: RateRequest | None = Field(
        default=None,
        description="Rate to the plan's base currency, for budgets and display only",
    )
    commitment_id: UUID | None = Field(
        default=None,
        description="The planned cost this expense pays for; it then counts once, as actual",
    )

    @model_validator(mode="after")
    def local_date_matches(self) -> Self:
        if (self.occurred_at is None) != (self.occurred_timezone is None):
            raise ValueError("send occurred_at and occurred_timezone together")
        if self.occurred_at is not None and self.occurred_timezone is not None:
            local = self.occurred_at.astimezone(ZoneInfo(self.occurred_timezone)).date()
            if local != self.occurred_on:
                raise ValueError("occurred_on must be the local date of occurred_at")
        return self


class ExpenseCreateRequest(ExpenseRequest):
    id: UUID | None = None


class RefundRecipient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID | None = None
    fund: bool = False

    @model_validator(mode="after")
    def one_account(self) -> Self:
        if self.fund == (self.participant_id is not None):
            raise ValueError("name a participant_id or set fund to true")
        return self


class RefundRequest(BaseModel):
    """Money that came back; by default it follows the current owed shares."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    amount_minor: StrictInt
    recipient: RefundRecipient
    shares: Annotated[list[ExactShare], Field(min_length=1, max_length=100)] | None = None
    note: Note | None = None


class PayerResponse(BaseModel):
    participant_id: UUID | None
    fund: bool
    amount_minor: int


class ShareResponse(BaseModel):
    participant_id: UUID
    owed_minor: int


class BaseAmountResponse(BaseModel):
    """A labelled snapshot in the plan's base currency; never rewrites postings."""

    currency: str
    amount_minor: int | None
    rate: str | None
    rate_source: Literal["identity", "manual", "estimated"] | None
    rate_as_of: datetime | None


class RefundShareResponse(BaseModel):
    participant_id: UUID
    amount_minor: int


class RefundResponse(BaseModel):
    id: UUID
    amount_minor: int
    currency: str
    recipient_participant_id: UUID | None
    fund: bool
    shares: list[RefundShareResponse]
    note: str | None
    reversed: bool
    created_by_user_id: UUID
    created_at: datetime


class RevisionResponse(BaseModel):
    id: UUID
    revision_number: int
    description: str
    category: ExpenseCategory
    occurred_on: date
    occurred_at: datetime | None
    occurred_timezone: str | None
    notes: str | None
    amount_minor: int
    currency: str
    payers: list[PayerResponse]
    split: Split
    split_algorithm: str
    shares: list[ShareResponse]
    personal: bool = Field(
        description="One person paid and is the only one sharing it; it moves no balance"
    )
    base: BaseAmountResponse
    commitment_id: UUID | None
    source: Literal["http", "sync"] = Field(description="Written online, or pushed by sync")
    client_created_at: datetime | None = Field(
        description="When the device made the change (offline edits sync later)"
    )
    device_label: str | None = Field(description="The device of the session that wrote it")
    created_by_user_id: UUID
    created_at: datetime


class ExpenseResponse(BaseModel):
    id: UUID
    plan_id: UUID
    state: Literal["active", "voided"]
    revision: RevisionResponse
    refunds: list[RefundResponse]
    refunded_minor: int
    created_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime
    voided_at: datetime | None


class LedgerBalanceResponse(BaseModel):
    participant_id: UUID | None
    fund: bool
    currency: str
    balance_minor: int = Field(description="Positive: should receive money; negative: owes")


class FundAvailabilityResponse(BaseModel):
    currency: str
    available_minor: int


class LedgerResponse(BaseModel):
    """The plan's balances per participant and currency, never netted across currencies."""

    plan_id: UUID
    status: Literal["open", "settled", "reopened"]
    ledger_seq: int
    disputed_settlements: int
    balances: list[LedgerBalanceResponse]
    fund: list[FundAvailabilityResponse] = Field(
        description="Money the participants recorded as pooled; Beluno holds and moves none"
    )
    version: int


SettlementMethod = Literal["cash", "bank_transfer", "card", "mobile_payment", "other"]


class PaidAmount(BaseModel):
    """What actually changed hands when it was another currency than the debt."""

    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode
    amount_minor: StrictInt


class SettlementRequest(BaseModel):
    """``from`` paid ``to``: the payer's balance rises and the receiver's falls."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    from_participant_id: UUID
    to_participant_id: UUID
    currency: CurrencyCode
    amount_minor: StrictInt
    paid: PaidAmount | None = None
    method: SettlementMethod | None = None
    fee_minor: StrictInt | None = Field(
        default=None, description="Bank or transfer fee the payer bore; not part of the debt"
    )
    note: Note | None = None
    occurred_on: date


class WaiverRequest(BaseModel):
    """The creditor forgives part of what the debtor owes; no money changes hands."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    debtor_participant_id: UUID
    creditor_participant_id: UUID
    currency: CurrencyCode
    amount_minor: StrictInt
    note: Note | None = None
    occurred_on: date


class PaidAmountResponse(BaseModel):
    currency: str
    amount_minor: int
    rate: str = Field(description="Paid-currency units per debt-currency unit the amounts imply")


class SettlementResponse(BaseModel):
    id: UUID
    plan_id: UUID
    kind: Literal["payment", "waiver"]
    from_participant_id: UUID
    to_participant_id: UUID
    currency: str
    amount_minor: int
    paid: PaidAmountResponse | None
    method: SettlementMethod | None
    fee_minor: int | None
    note: str | None
    occurred_on: date
    status: Literal["recorded", "confirmed", "disputed", "reversed"]
    overpaid: bool
    recorded_by_user_id: UUID
    confirmed_by_user_id: UUID | None
    confirmed_at: datetime | None
    disputed_by_user_id: UUID | None
    disputed_at: datetime | None
    reversed_by_user_id: UUID | None
    reversed_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


TransactionKind = Literal[
    "expense",
    "expense_reversal",
    "refund",
    "settlement",
    "settlement_reversal",
    "fund_contribution",
    "fund_withdrawal",
    "adjustment",
    "conversion",
]


class PostingResponse(BaseModel):
    participant_id: UUID | None
    fund: bool
    currency: str
    amount_minor: int


class TransactionResponse(BaseModel):
    """One balanced journal entry; its postings sum to zero in each currency."""

    id: UUID
    ledger_seq: int
    kind: TransactionKind
    subtype: Literal["waiver", "merge_transfer", "fund_adjustment", "correction"] | None
    expense_id: UUID | None
    refund_id: UUID | None
    settlement_id: UUID | None
    fund_movement_id: UUID | None
    reverses_transaction_id: UUID | None
    memo: str | None
    created_by_user_id: UUID | None
    created_at: datetime
    postings: list[PostingResponse]


class TransactionPage(BaseModel):
    items: list[TransactionResponse]
    next_cursor: str | None = None


class ExplanationEntry(BaseModel):
    transaction_id: UUID
    ledger_seq: int
    kind: TransactionKind
    subtype: Literal["waiver", "merge_transfer", "fund_adjustment", "correction"] | None
    expense_id: UUID | None
    settlement_id: UUID | None
    fund_movement_id: UUID | None
    description: str | None
    amount_minor: int
    balance_after_minor: int
    created_at: datetime


class BalanceExplanation(BaseModel):
    participant_id: UUID | None
    fund: bool
    currency: str
    entries: list[ExplanationEntry]
    next_cursor: str | None = None


class SuggestedTransfer(BaseModel):
    from_participant_id: UUID
    to_participant_id: UUID
    amount_minor: int


class SuggestedFundPayout(BaseModel):
    to_participant_id: UUID
    amount_minor: int


class SettlementPreviewResponse(BaseModel):
    """A suggestion only: nothing is recorded until someone records a settlement."""

    currency: str
    transfers: list[SuggestedTransfer]
    fund_payouts: list[SuggestedFundPayout]


BudgetScope = Literal["total", "category", "participant", "daily"]
CommitmentStateName = Literal[
    "estimated", "committed", "converted_to_expense", "cancelled", "refunded"
]


class BudgetCreateRequest(BaseModel):
    """A limit in the plan's base currency; ``daily`` applies to every day."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    scope: BudgetScope
    category: ExpenseCategory | None = None
    participant_id: UUID | None = None
    limit_minor: StrictInt


class BudgetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit_minor: StrictInt


class BudgetResponse(BaseModel):
    id: UUID
    plan_id: UUID
    scope: BudgetScope
    category: ExpenseCategory | None
    participant_id: UUID | None
    currency: str
    limit_minor: int
    version: int
    created_at: datetime
    updated_at: datetime


class SpendResponse(BaseModel):
    actual_minor: int
    committed_minor: int
    estimated_minor: int
    projected_minor: int = Field(description="Actual plus committed plus estimated")


class DaySpend(BaseModel):
    date: date
    actual_minor: int


class BudgetUsageResponse(BaseModel):
    budget: BudgetResponse
    spend: SpendResponse
    remaining_minor: int
    over_limit: bool
    days: list[DaySpend]


class CategorySpend(BaseModel):
    category: ExpenseCategory
    spend: SpendResponse


class UnconvertedSpend(BaseModel):
    """Spend or planned cost in another currency without a rate; not in the totals."""

    tier: Literal["actual", "committed", "estimated"]
    currency: str
    amount_minor: int


class BudgetOverviewResponse(BaseModel):
    currency: str
    total: SpendResponse
    categories: list[CategorySpend]
    unconverted: list[UnconvertedSpend]
    estimated_rates: bool = Field(description="Some totals use offline estimated rates")
    budgets: list[BudgetUsageResponse]


class CommitmentCreateRequest(BaseModel):
    """A planned cost; link it from the expense that pays it to count it once."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    category: ExpenseCategory = "other"
    description: Description
    currency: CurrencyCode
    amount_minor: StrictInt
    state: Literal["estimated", "committed"] = "estimated"
    base_rate: RateRequest | None = None


class CommitmentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ExpenseCategory = "other"
    description: Description
    currency: CurrencyCode
    amount_minor: StrictInt
    state: Literal["estimated", "committed", "converted_to_expense", "cancelled", "refunded"]
    base_rate: RateRequest | None = None


class CommitmentResponse(BaseModel):
    id: UUID
    plan_id: UUID
    source_type: Literal["manual", "booking", "itinerary_item", "place", "responsibility"]
    source_id: UUID
    commitment_kind: str
    state: CommitmentStateName
    category: ExpenseCategory
    description: str
    currency: str
    amount_minor: int
    base: BaseAmountResponse
    expense_id: UUID | None
    created_by_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


FUND_NOTICE = (
    "Beluno records what participants pooled with a custodian; it never holds, moves, "
    "or stores money."
)


class FundSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custodian_participant_id: UUID | None = None
    note: Note | None = None


class FundSettingsResponse(BaseModel):
    plan_id: UUID
    custodian_participant_id: UUID | None
    note: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class FundMovementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    participant_id: UUID
    currency: CurrencyCode
    amount_minor: StrictInt
    note: Note | None = None
    occurred_on: date


class FundMovementResponse(BaseModel):
    id: UUID
    plan_id: UUID
    kind: Literal["contribution", "withdrawal"]
    participant_id: UUID
    currency: str
    amount_minor: int
    note: str | None
    occurred_on: date
    created_by_user_id: UUID
    created_at: datetime


class FundResponse(BaseModel):
    settings: FundSettingsResponse | None
    available: list[FundAvailabilityResponse]
    notice: str = Field(default=FUND_NOTICE)


class AdjustmentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID | None = None
    fund: bool = False
    amount_minor: StrictInt

    @model_validator(mode="after")
    def one_account(self) -> Self:
        if self.fund == (self.participant_id is not None):
            raise ValueError("name a participant_id or set fund to true")
        return self


class AdjustmentRequest(BaseModel):
    """A privileged correction in one currency; entries must sum to zero."""

    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode
    memo: Note
    entries: Annotated[list[AdjustmentEntry], Field(min_length=2, max_length=100)]
