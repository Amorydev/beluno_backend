"""Financial ledger rows.

Canonical history (accounts, FX snapshots, revisions, payers, splits, refunds,
fund movements, transactions, postings) is insert-only; triggers reject updates
and deletes, and deferred constraint triggers verify every entry at commit.
Heads, balances, expenses, settlements, budgets, commitments, and fund settings
are the mutable rows.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beluno.db.models.base import Base

SCHEMA = {"schema": "finance"}


class Currency(Base):
    __tablename__ = "currencies"
    __table_args__ = SCHEMA

    code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    exponent: Mapped[int] = mapped_column(SmallInteger)
    state: Mapped[str] = mapped_column(Text)
    metadata_version: Mapped[int]
    updated_at: Mapped[datetime]


class LedgerHead(Base):
    """One per plan: the ledger sequence, status, and the lock every finance write takes."""

    __tablename__ = "plan_ledger_heads"
    __table_args__ = SCHEMA

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_seq: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)
    disputed_settlements: Mapped[int]
    # Money settings: whether budgets count personal spend, and the base-currency
    # balance below which a person counts as settled.
    count_personal_spend: Mapped[bool]
    settle_tolerance_minor: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class LedgerConfirmation(Base):
    """A participant said the ledger looked right at one sequence (append-only)."""

    __tablename__ = "ledger_confirmations"
    __table_args__ = SCHEMA

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    confirmed_by_user_id: Mapped[UUID]
    confirmed_at: Mapped[datetime]


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    participant_id: Mapped[UUID | None]
    currency: Mapped[str] = mapped_column(CHAR(3))
    created_at: Mapped[datetime]


class AccountBalance(Base):
    """Projection: the running balance of one account, rebuilt from postings at will."""

    __tablename__ = "account_balances"
    __table_args__ = SCHEMA

    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(CHAR(3))
    balance_minor: Mapped[int] = mapped_column(BigInteger)
    last_ledger_seq: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime]


class FxSnapshot(Base):
    __tablename__ = "fx_snapshots"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    base_currency: Mapped[str] = mapped_column(CHAR(3))
    quote_currency: Mapped[str] = mapped_column(CHAR(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    source: Mapped[str] = mapped_column(Text)
    rounding_mode: Mapped[str] = mapped_column(Text)
    as_of: Mapped[datetime]
    created_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]


class CostCommitment(Base):
    __tablename__ = "cost_commitments"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[UUID]
    commitment_kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    converted_from_state: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(CHAR(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    base_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    base_fx_snapshot_id: Mapped[UUID | None]
    expense_id: Mapped[UUID | None]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Expense(Base):
    """Stable expense identity; its value lives in immutable revisions."""

    __tablename__ = "expenses"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(Text)
    current_revision_id: Mapped[UUID]
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    voided_at: Mapped[datetime | None]


class ExpenseRevision(Base):
    __tablename__ = "expense_revisions"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    expense_id: Mapped[UUID]
    revision_number: Mapped[int]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    occurred_on: Mapped[date]
    occurred_at: Mapped[datetime | None]
    occurred_timezone: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    split_method: Mapped[str] = mapped_column(Text)
    split_algorithm: Mapped[str] = mapped_column(Text)
    split_input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    base_currency: Mapped[str] = mapped_column(CHAR(3))
    base_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    base_fx_snapshot_id: Mapped[UUID | None]
    commitment_id: Mapped[UUID | None]
    source: Mapped[str] = mapped_column(Text)
    client_created_at: Mapped[datetime | None]
    device_label: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]


class ExpensePayer(Base):
    __tablename__ = "expense_payers"
    __table_args__ = SCHEMA

    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    plan_id: Mapped[UUID]
    participant_id: Mapped[UUID | None]
    amount_minor: Mapped[int] = mapped_column(BigInteger)


class ExpenseSplit(Base):
    __tablename__ = "expense_splits"
    __table_args__ = SCHEMA

    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    plan_id: Mapped[UUID]
    participant_id: Mapped[UUID]
    owed_minor: Mapped[int] = mapped_column(BigInteger)


class ExpenseRefund(Base):
    __tablename__ = "expense_refunds"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    expense_id: Mapped[UUID]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3))
    recipient_participant_id: Mapped[UUID | None]
    note: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]


class RefundShare(Base):
    __tablename__ = "refund_shares"
    __table_args__ = SCHEMA

    refund_id: Mapped[UUID] = mapped_column(primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    amount_minor: Mapped[int] = mapped_column(BigInteger)


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    from_participant_id: Mapped[UUID]
    to_participant_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(CHAR(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    paid_currency: Mapped[str | None] = mapped_column(CHAR(3))
    paid_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    fx_snapshot_id: Mapped[UUID | None]
    method: Mapped[str | None] = mapped_column(Text)
    fee_minor: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    occurred_on: Mapped[date]
    status: Mapped[str] = mapped_column(Text)
    overpaid: Mapped[bool]
    recorded_by_user_id: Mapped[UUID]
    confirmed_by_user_id: Mapped[UUID | None]
    confirmed_at: Mapped[datetime | None]
    disputed_by_user_id: Mapped[UUID | None]
    disputed_at: Mapped[datetime | None]
    reversed_by_user_id: Mapped[UUID | None]
    reversed_at: Mapped[datetime | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class FundSettings(Base):
    __tablename__ = "fund_settings"
    __table_args__ = SCHEMA

    plan_id: Mapped[UUID] = mapped_column(primary_key=True)
    custodian_participant_id: Mapped[UUID | None]
    note: Mapped[str | None] = mapped_column(Text)
    # What each member is asked to put in ("¥5,000 of ¥10,000").
    target_currency: Mapped[str | None] = mapped_column(CHAR(3))
    target_minor: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class FundCount(Base):
    """Cash counted in the kitty against what the ledger expected; posts nothing."""

    __tablename__ = "fund_counts"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(CHAR(3))
    counted_minor: Mapped[int] = mapped_column(BigInteger)
    expected_minor: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    counted_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]


class FundMovement(Base):
    __tablename__ = "fund_movements"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(Text)
    participant_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(CHAR(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    occurred_on: Mapped[date]
    created_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    ledger_seq: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)
    subtype: Mapped[str | None] = mapped_column(Text)
    expense_id: Mapped[UUID | None]
    revision_id: Mapped[UUID | None]
    refund_id: Mapped[UUID | None]
    settlement_id: Mapped[UUID | None]
    fund_movement_id: Mapped[UUID | None]
    consolidation_id: Mapped[UUID | None]
    reverses_transaction_id: Mapped[UUID | None]
    memo: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID | None]
    operation_id: Mapped[UUID | None]
    created_at: Mapped[datetime]


class LedgerPosting(Base):
    __tablename__ = "ledger_postings"
    __table_args__ = SCHEMA

    transaction_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(CHAR(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    scope: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    participant_id: Mapped[UUID | None]
    currency: Mapped[str] = mapped_column(CHAR(3))
    limit_minor: Mapped[int] = mapped_column(BigInteger)
    created_by_user_id: Mapped[UUID]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class MarketRate(Base):
    """A market rate a provider published (reference data, no tenant, append-only)."""

    __tablename__ = "market_rates"
    __table_args__ = SCHEMA

    base_currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    quote_currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    source: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime]


class Consolidation(Base):
    """Every foreign-currency balance converted into the base currency at frozen rates."""

    __tablename__ = "consolidations"
    __table_args__ = SCHEMA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    base_currency: Mapped[str] = mapped_column(CHAR(3))
    state: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[UUID]
    created_at: Mapped[datetime]
    reversed_by_user_id: Mapped[UUID | None]
    reversed_at: Mapped[datetime | None]
    version: Mapped[int]
    updated_at: Mapped[datetime]


class ConsolidationRate(Base):
    __tablename__ = "consolidation_rates"
    __table_args__ = SCHEMA

    consolidation_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    plan_id: Mapped[UUID]
    fx_snapshot_id: Mapped[UUID]


class ConsolidationLine(Base):
    """One participant's balance in one currency and the base amount it became."""

    __tablename__ = "consolidation_lines"
    __table_args__ = SCHEMA

    consolidation_id: Mapped[UUID] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    participant_id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_id: Mapped[UUID]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger)
