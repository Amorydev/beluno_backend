"""Financial ledger contracts: currencies, expenses, refunds, and the ledger view.

Amounts are integers in minor units of the named currency (``amount_minor``);
the currency's exponent comes from ``GET /v1/currencies``. Exchange rates travel
as decimal strings so no client ever parses them as floating point.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

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

from beluno.contracts.common import CurrencyCode, Label, LongText, clean_text

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


Split = Annotated[
    EqualSplit | ExactSplit | PercentageSplit | SharesSplit | ItemizedSplit,
    Field(discriminator="method"),
]


class ExpenseRequest(BaseModel):
    """A full expense value: creating it or replacing it appends a revision."""

    model_config = ConfigDict(extra="forbid")

    description: Description
    category: ExpenseCategory = "other"
    occurred_on: date
    notes: LongText | None = None
    amount_minor: StrictInt
    currency: CurrencyCode
    payers: Annotated[list[PayerRequest], Field(min_length=1, max_length=100)]
    split: Split
    base_rate: RateRequest | None = Field(
        default=None,
        description="Rate to the plan's base currency, for budgets and display only",
    )


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
    notes: str | None
    amount_minor: int
    currency: str
    payers: list[PayerResponse]
    split: Split
    split_algorithm: str
    shares: list[ShareResponse]
    base: BaseAmountResponse
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
