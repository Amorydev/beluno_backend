"""Deterministic expense splitting (algorithm ``lr-v1``).

Every method reduces to integer weights and the largest-remainder rule:

1. each party gets ``floor(amount * weight / total_weight)`` minor units;
2. the units left over go, one each, to the parties with the largest
   fractional remainders;
3. equal remainders go to the party captured earlier in the request.

Inputs are integers (weights, basis points, exact minor units), so the result
is the same on every server and client that implements the same version.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from beluno.modules.finance.errors import split_invalid
from beluno.modules.finance.money import (
    BASIS_POINTS_TOTAL,
    MAX_EXTRAS,
    MAX_ITEMS,
    MAX_PAYERS,
    MAX_SPLIT_PARTICIPANTS,
    MAX_WEIGHT,
    check_amount,
)

SPLIT_ALGORITHM = "lr-v1"


class SplitMethod(StrEnum):
    EQUAL = "equal"
    EXACT = "exact"
    PERCENTAGE = "percentage"
    SHARES = "shares"
    ITEMIZED = "itemized"


@dataclass(frozen=True)
class SplitEntry:
    """One party of a non-itemized split.

    ``value`` is ignored for ``equal``; it is the exact owed amount for ``exact``,
    basis points for ``percentage``, and an integer weight for ``shares``.
    """

    participant_id: UUID
    value: int = 1


@dataclass(frozen=True)
class SplitItem:
    """One line of an itemized bill, split equally or by ``weights``."""

    amount_minor: int
    participant_ids: tuple[UUID, ...]
    weights: tuple[int, ...] | None = None


@dataclass(frozen=True)
class SplitSpec:
    method: SplitMethod
    entries: tuple[SplitEntry, ...] = ()
    items: tuple[SplitItem, ...] = ()
    # Shared extras (tax, tip, service) spread over the item subtotals.
    extras: tuple[int, ...] = ()


@dataclass(frozen=True)
class Share:
    participant_id: UUID
    owed_minor: int


@dataclass(frozen=True)
class Payer:
    """A payer is a participant, or the plan fund when ``participant_id`` is None."""

    participant_id: UUID | None
    amount_minor: int


def largest_remainder(total: int, weights: Sequence[int]) -> list[int]:
    """Allocate ``total`` minor units proportionally to ``weights`` (``lr-v1``)."""

    if total < 0:
        raise ValueError("total must not be negative")
    if not weights or any(weight < 0 for weight in weights):
        raise ValueError("weights must be non-negative and non-empty")
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("weights must not all be zero")
    products = [total * weight for weight in weights]
    allocation = [product // weight_sum for product in products]
    leftover = total - sum(allocation)
    by_remainder = sorted(
        range(len(weights)), key=lambda index: (-(products[index] % weight_sum), index)
    )
    for index in by_remainder[:leftover]:
        allocation[index] += 1
    return allocation


def resolve_split(amount_minor: int, spec: SplitSpec) -> list[Share]:
    """Owed minor units per participant, in first-captured order, summing to the amount."""

    check_amount(amount_minor, field="amount_minor")
    if spec.method is SplitMethod.ITEMIZED:
        return _itemized(amount_minor, spec)
    entries = spec.entries
    if not entries:
        raise split_invalid("a split needs at least one participant")
    if len(entries) > MAX_SPLIT_PARTICIPANTS:
        raise split_invalid(f"a split takes at most {MAX_SPLIT_PARTICIPANTS} participants")
    participants = [entry.participant_id for entry in entries]
    _require_unique(participants)
    if spec.method is SplitMethod.EQUAL:
        owed = largest_remainder(amount_minor, [1] * len(entries))
    elif spec.method is SplitMethod.EXACT:
        owed = [entry.value for entry in entries]
        if any(value < 0 for value in owed) or sum(owed) != amount_minor:
            raise split_invalid("exact shares must be non-negative and add up to the amount")
    elif spec.method is SplitMethod.PERCENTAGE:
        points = [entry.value for entry in entries]
        if any(value <= 0 for value in points) or sum(points) != BASIS_POINTS_TOTAL:
            raise split_invalid("percentages must be positive and add up to exactly 100")
        owed = largest_remainder(amount_minor, points)
    else:
        weights = [entry.value for entry in entries]
        if any(value <= 0 or value > MAX_WEIGHT for value in weights):
            raise split_invalid(f"weights must be whole numbers from 1 to {MAX_WEIGHT}")
        owed = largest_remainder(amount_minor, weights)
    return [
        Share(participant, value) for participant, value in zip(participants, owed, strict=True)
    ]


def _itemized(amount_minor: int, spec: SplitSpec) -> list[Share]:
    if not spec.items or len(spec.items) > MAX_ITEMS:
        raise split_invalid(f"an itemized split needs 1 to {MAX_ITEMS} items")
    if len(spec.extras) > MAX_EXTRAS:
        raise split_invalid(f"an itemized split takes at most {MAX_EXTRAS} extras")
    for item in spec.items:
        check_amount(item.amount_minor, field="item amount_minor")
    for extra in spec.extras:
        check_amount(extra, field="extra amount_minor")
    if sum(item.amount_minor for item in spec.items) + sum(spec.extras) != amount_minor:
        raise split_invalid("items and extras must add up to the amount")
    subtotals: dict[UUID, int] = {}
    for item in spec.items:
        if not item.participant_ids:
            raise split_invalid("every item needs at least one participant")
        _require_unique(list(item.participant_ids))
        weights = (
            list(item.weights) if item.weights is not None else [1] * len(item.participant_ids)
        )
        if len(weights) != len(item.participant_ids):
            raise split_invalid("item weights must match its participants")
        if any(weight <= 0 or weight > MAX_WEIGHT for weight in weights):
            raise split_invalid(f"weights must be whole numbers from 1 to {MAX_WEIGHT}")
        for participant, owed in zip(
            item.participant_ids, largest_remainder(item.amount_minor, weights), strict=True
        ):
            subtotals[participant] = subtotals.get(participant, 0) + owed
    if len(subtotals) > MAX_SPLIT_PARTICIPANTS:
        raise split_invalid(f"a split takes at most {MAX_SPLIT_PARTICIPANTS} participants")
    participants = list(subtotals)
    extras = sum(spec.extras)
    if extras:
        base = [subtotals[participant] for participant in participants]
        for participant, owed in zip(participants, largest_remainder(extras, base), strict=True):
            subtotals[participant] += owed
    return [Share(participant, subtotals[participant]) for participant in participants]


def validate_payers(amount_minor: int, payers: Sequence[Payer]) -> list[Payer]:
    """Payers are unique accounts with positive amounts that add up to the expense."""

    if not payers or len(payers) > MAX_PAYERS:
        raise split_invalid(f"an expense needs 1 to {MAX_PAYERS} payers")
    _require_unique([payer.participant_id for payer in payers])
    for payer in payers:
        check_amount(payer.amount_minor, field="payer amount_minor")
    if sum(payer.amount_minor for payer in payers) != amount_minor:
        raise split_invalid("payer amounts must add up to the expense amount")
    return list(payers)


def _require_unique(keys: Sequence[UUID | None]) -> None:
    if len(set(keys)) != len(keys):
        raise split_invalid("each participant may appear only once")
