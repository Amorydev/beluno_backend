"""Balanced posting sets for every kind of ledger entry.

A posting set maps a party (a participant or the plan fund) to a non-zero
signed amount in one currency, and always sums to zero. Positive means the
party should receive money; negative means it owes or holds money.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from beluno.modules.finance.splits import Payer, Share, largest_remainder

FUND_SORT_KEY = ""


@dataclass(frozen=True)
class Party:
    """A participant account (``participant_id`` set) or the fund account (None)."""

    participant_id: UUID | None

    @property
    def is_fund(self) -> bool:
        return self.participant_id is None

    @property
    def sort_key(self) -> str:
        return FUND_SORT_KEY if self.participant_id is None else str(self.participant_id)


FUND = Party(None)

Postings = dict[Party, int]


def _collect(amounts: Iterable[tuple[Party, int]]) -> Postings:
    totals: dict[Party, int] = {}
    for party, amount in amounts:
        totals[party] = totals.get(party, 0) + amount
    ordered = sorted(totals.items(), key=lambda pair: pair[0].sort_key)
    postings = {party: amount for party, amount in ordered if amount != 0}
    if sum(postings.values()) != 0:
        raise ValueError("postings must sum to zero")
    return postings


def expense_postings(payers: Sequence[Payer], shares: Sequence[Share]) -> Postings:
    """``paid - owed`` per party; a party that paid exactly its share has no posting."""

    paid = ((Party(payer.participant_id), payer.amount_minor) for payer in payers)
    owed = ((Party(share.participant_id), -share.owed_minor) for share in shares)
    return _collect([*paid, *owed])


def reversal_postings(
    original: Mapping[Party, int], resolve: Mapping[Party, Party] | None = None
) -> Postings:
    """The exact negation of an entry, moved to surviving parties when one was merged."""

    resolve = resolve or {}
    return _collect((resolve.get(party, party), -amount) for party, amount in original.items())


def refund_allocation(amount_minor: int, shares: Sequence[Share]) -> list[Share]:
    """Spread a refund over the current owed shares (largest remainder, captured order)."""

    owing = [share for share in shares if share.owed_minor > 0]
    allocated = largest_remainder(amount_minor, [share.owed_minor for share in owing])
    return [
        Share(share.participant_id, value)
        for share, value in zip(owing, allocated, strict=True)
        if value > 0
    ]


def refund_postings(recipient: Party, shares: Sequence[Share]) -> Postings:
    """The recipient got money back (``-amount``); the people who bore the cost owe less."""

    total = sum(share.owed_minor for share in shares)
    back = ((Party(share.participant_id), share.owed_minor) for share in shares)
    return _collect([(recipient, -total), *back])


def transfer_postings(payer: Party, receiver: Party, amount_minor: int) -> Postings:
    """Money handed from ``payer`` to ``receiver`` (settlements, waivers, fund flows).

    The payer's position improves by the amount and the receiver's drops by it:
    a settlement from debtor to creditor, a contribution from a participant to the
    fund, or a withdrawal from the fund to a participant.
    """

    if payer == receiver:
        raise ValueError("a transfer needs two different parties")
    return _collect([(payer, amount_minor), (receiver, -amount_minor)])


def adjustment_postings(entries: Iterable[tuple[Party, int]]) -> Postings:
    return _collect(entries)


def consolidation_amounts(balances: Mapping[UUID, int], converted_total: int) -> dict[UUID, int]:
    """Base-currency amounts for one currency's balances, summing to exactly zero.

    ``balances`` sum to zero (the fund holds nothing in that currency), and
    ``converted_total`` is what the creditors' side converts to. Creditors share
    it by their balances and debtors share the same total by theirs, each with
    the largest-remainder rule in participant-id order, so every amount is within
    about a unit of its exact value and the base side stays zero-sum.
    """

    if sum(balances.values()) != 0:
        raise ValueError("balances to consolidate must sum to zero")
    if converted_total < 0:
        raise ValueError("the converted total cannot be negative")
    ordered = sorted((pid for pid, value in balances.items() if value), key=str)
    creditors = [pid for pid in ordered if balances[pid] > 0]
    debtors = [pid for pid in ordered if balances[pid] < 0]
    amounts: dict[UUID, int] = {}
    if creditors:
        for pid, value in zip(
            creditors,
            largest_remainder(converted_total, [balances[pid] for pid in creditors]),
            strict=True,
        ):
            amounts[pid] = value
        for pid, value in zip(
            debtors,
            largest_remainder(converted_total, [-balances[pid] for pid in debtors]),
            strict=True,
        ):
            amounts[pid] = -value
    return amounts
