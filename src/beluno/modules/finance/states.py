"""State rules for ledgers, settlements, and cost commitments."""

from __future__ import annotations

from enum import StrEnum


class LedgerStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    REOPENED = "reopened"


def next_ledger_status(
    current: LedgerStatus, *, balances_zero: bool, has_live_settlement: bool
) -> LedgerStatus:
    """``settled`` once everyone is square after a settlement; later drift reopens it.

    A ledger with nothing owed and no live settlement (everything voided or
    reversed) is simply ``open`` again.
    """

    if balances_zero:
        return LedgerStatus.SETTLED if has_live_settlement else LedgerStatus.OPEN
    if current is LedgerStatus.OPEN:
        return LedgerStatus.OPEN
    return LedgerStatus.REOPENED


class SettlementStatus(StrEnum):
    RECORDED = "recorded"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    REVERSED = "reversed"


SETTLEMENT_TRANSITIONS: dict[SettlementStatus, frozenset[SettlementStatus]] = {
    SettlementStatus.RECORDED: frozenset(
        {SettlementStatus.CONFIRMED, SettlementStatus.DISPUTED, SettlementStatus.REVERSED}
    ),
    SettlementStatus.CONFIRMED: frozenset({SettlementStatus.REVERSED}),
    SettlementStatus.DISPUTED: frozenset({SettlementStatus.CONFIRMED, SettlementStatus.REVERSED}),
    SettlementStatus.REVERSED: frozenset(),
}


class CommitmentState(StrEnum):
    ESTIMATED = "estimated"
    COMMITTED = "committed"
    CONVERTED = "converted_to_expense"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


# States a commitment owner may set directly; conversion only happens by linking
# an expense, and a refund only follows a conversion.
COMMITMENT_TRANSITIONS: dict[CommitmentState, frozenset[CommitmentState]] = {
    CommitmentState.ESTIMATED: frozenset({CommitmentState.COMMITTED, CommitmentState.CANCELLED}),
    CommitmentState.COMMITTED: frozenset({CommitmentState.ESTIMATED, CommitmentState.CANCELLED}),
    CommitmentState.CANCELLED: frozenset({CommitmentState.ESTIMATED, CommitmentState.COMMITTED}),
    CommitmentState.CONVERTED: frozenset({CommitmentState.REFUNDED}),
    CommitmentState.REFUNDED: frozenset(),
}

LINKABLE_COMMITMENT_STATES = frozenset({CommitmentState.ESTIMATED, CommitmentState.COMMITTED})


class BudgetTier(StrEnum):
    ACTUAL = "actual"
    COMMITTED = "committed"
    ESTIMATED = "estimated"


def commitment_tier(state: CommitmentState) -> BudgetTier | None:
    """The single tier a commitment counts in; converted ones count through their expense."""

    if state is CommitmentState.COMMITTED:
        return BudgetTier.COMMITTED
    if state is CommitmentState.ESTIMATED:
        return BudgetTier.ESTIMATED
    return None
