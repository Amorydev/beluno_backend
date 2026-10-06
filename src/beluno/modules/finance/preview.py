"""Deterministic debt simplification for one currency.

The preview only suggests transfers; nothing is posted until participants
record a settlement. Largest debtor pays largest creditor first; equal amounts
are ordered by participant id, so every caller sees the same suggestion. Money
still held by the plan fund is suggested as fund payouts to the remaining
creditors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Transfer:
    from_participant_id: UUID
    to_participant_id: UUID
    amount_minor: int


@dataclass(frozen=True)
class FundPayout:
    to_participant_id: UUID
    amount_minor: int


@dataclass(frozen=True)
class SettlementPreview:
    transfers: list[Transfer]
    fund_payouts: list[FundPayout]


def simplify_debts(balances: Mapping[UUID, int], fund_available: int = 0) -> SettlementPreview:
    """At most ``n - 1`` transfers (plus payouts) that bring every balance to zero."""

    if fund_available < 0:
        raise ValueError("fund availability cannot be negative")
    if sum(balances.values()) != fund_available:
        raise ValueError("participant balances must add up to the money held by the fund")
    creditors = {pid: amount for pid, amount in balances.items() if amount > 0}
    debtors = {pid: -amount for pid, amount in balances.items() if amount < 0}
    transfers: list[Transfer] = []
    while debtors:
        debtor = _largest(debtors)
        creditor = _largest(creditors)
        amount = min(debtors[debtor], creditors[creditor])
        transfers.append(Transfer(debtor, creditor, amount))
        _reduce(debtors, debtor, amount)
        _reduce(creditors, creditor, amount)
    payouts = [
        FundPayout(pid, creditors[pid])
        for pid in sorted(creditors, key=lambda p: (-creditors[p], str(p)))
    ]
    return SettlementPreview(transfers=transfers, fund_payouts=payouts)


def _largest(amounts: Mapping[UUID, int]) -> UUID:
    return min(amounts, key=lambda pid: (-amounts[pid], str(pid)))


def _reduce(amounts: dict[UUID, int], key: UUID, by: int) -> None:
    amounts[key] -= by
    if amounts[key] == 0:
        del amounts[key]
