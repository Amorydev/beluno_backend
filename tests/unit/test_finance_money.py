"""Money kernel: splits, payers, conversion, postings, debt preview, and state rules."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from beluno.contracts.errors import BelunoError
from beluno.modules.finance.fx import convert, implied_rate, parse_rate
from beluno.modules.finance.money import MAX_AMOUNT_MINOR, check_amount
from beluno.modules.finance.postings import (
    FUND,
    Party,
    expense_postings,
    refund_allocation,
    refund_postings,
    reversal_postings,
    transfer_postings,
)
from beluno.modules.finance.preview import simplify_debts
from beluno.modules.finance.splits import (
    Payer,
    Share,
    SplitEntry,
    SplitItem,
    SplitMethod,
    SplitSpec,
    largest_remainder,
    resolve_split,
    validate_payers,
)
from beluno.modules.finance.states import (
    BudgetTier,
    CommitmentState,
    LedgerStatus,
    commitment_tier,
    next_ledger_status,
)

PEOPLE = [UUID(int=index) for index in range(1, 201)]


def entries(*values: int) -> tuple[SplitEntry, ...]:
    return tuple(SplitEntry(PEOPLE[index], value) for index, value in enumerate(values))


def owed(shares: list[Share]) -> list[int]:
    return [share.owed_minor for share in shares]


def code(error: pytest.ExceptionInfo[BelunoError]) -> str:
    return error.value.code


# --- golden cases ------------------------------------------------------------------


def test_one_yen_across_five_people_goes_to_the_first_captured() -> None:
    spec = SplitSpec(SplitMethod.EQUAL, entries(1, 1, 1, 1, 1))
    assert owed(resolve_split(1, spec)) == [1, 0, 0, 0, 0]


def test_ten_dollars_one_cent_by_weights() -> None:
    spec = SplitSpec(SplitMethod.SHARES, entries(1, 2, 3))
    assert owed(resolve_split(1001, spec)) == [167, 334, 500]


def test_kuwaiti_dinar_three_decimals_split_equally() -> None:
    spec = SplitSpec(SplitMethod.EQUAL, entries(1, 1, 1))
    assert owed(resolve_split(10_000, spec)) == [3334, 3333, 3333]


def test_percentages_use_basis_points_and_must_total_one_hundred() -> None:
    spec = SplitSpec(SplitMethod.PERCENTAGE, entries(3334, 3333, 3333))
    assert owed(resolve_split(10_000, spec)) == [3334, 3333, 3333]
    with pytest.raises(BelunoError) as error:
        resolve_split(10_000, SplitSpec(SplitMethod.PERCENTAGE, entries(5000, 4999)))
    assert code(error) == "SPLIT_INVALID"


def test_exact_shares_must_add_up() -> None:
    assert owed(resolve_split(900, SplitSpec(SplitMethod.EXACT, entries(500, 400, 0)))) == [
        500,
        400,
        0,
    ]
    with pytest.raises(BelunoError) as error:
        resolve_split(900, SplitSpec(SplitMethod.EXACT, entries(500, 300)))
    assert code(error) == "SPLIT_INVALID"


def test_adjustments_shift_equal_shares_by_exact_minor_units() -> None:
    adjusted = SplitSpec(SplitMethod.ADJUSTMENT, entries(100, 0, -100))
    assert owed(resolve_split(1_000, adjusted)) == [434, 333, 233]
    # Adjustments beyond the amount share the negative rest the same way.
    over = SplitSpec(SplitMethod.ADJUSTMENT, entries(80, 80))
    assert owed(resolve_split(100, over)) == [50, 50]
    uneven = SplitSpec(SplitMethod.ADJUSTMENT, entries(12, 12, 1))
    assert owed(resolve_split(20, uneven)) == [10, 10, 0]
    with pytest.raises(BelunoError) as negative:
        resolve_split(100, SplitSpec(SplitMethod.ADJUSTMENT, entries(0, 200)))
    assert code(negative) == "SPLIT_INVALID"
    with pytest.raises(BelunoError) as huge:
        resolve_split(100, SplitSpec(SplitMethod.ADJUSTMENT, entries(MAX_AMOUNT_MINOR + 1, 0)))
    assert code(huge) == "SPLIT_INVALID"


def test_itemized_bill_spreads_tax_and_tip_over_item_subtotals() -> None:
    alice, bob, carol = PEOPLE[:3]
    spec = SplitSpec(
        SplitMethod.ITEMIZED,
        items=(
            SplitItem(3000, (alice,)),
            SplitItem(1000, (bob, carol)),
            SplitItem(2000, (alice, bob, carol), weights=(2, 1, 1)),
        ),
        extras=(600, 400),
    )
    shares = resolve_split(7000, spec)
    # Subtotals 4000 / 1000 / 1000; extras 1000 follow them: 667 / 167 / 166.
    assert [(s.participant_id, s.owed_minor) for s in shares] == [
        (alice, 4667),
        (bob, 1167),
        (carol, 1166),
    ]


def test_itemized_items_and_extras_must_cover_the_amount() -> None:
    spec = SplitSpec(SplitMethod.ITEMIZED, items=(SplitItem(500, (PEOPLE[0],)),), extras=(10,))
    with pytest.raises(BelunoError) as error:
        resolve_split(600, spec)
    assert code(error) == "SPLIT_INVALID"


@pytest.mark.parametrize(
    "spec",
    [
        SplitSpec(SplitMethod.EQUAL, ()),
        SplitSpec(SplitMethod.EQUAL, (SplitEntry(PEOPLE[0]), SplitEntry(PEOPLE[0]))),
        SplitSpec(SplitMethod.SHARES, entries(0, 1)),
        SplitSpec(SplitMethod.SHARES, entries(1, 1_000_001)),
        SplitSpec(SplitMethod.EQUAL, entries(*([1] * 101))),
        SplitSpec(SplitMethod.ITEMIZED, items=(SplitItem(100, ()),)),
        SplitSpec(SplitMethod.ITEMIZED, items=(SplitItem(100, (PEOPLE[0],), weights=(1, 2)),)),
    ],
)
def test_invalid_splits_are_rejected(spec: SplitSpec) -> None:
    with pytest.raises(BelunoError) as error:
        resolve_split(100, spec)
    assert code(error) == "SPLIT_INVALID"


@pytest.mark.parametrize("amount", [0, -1, MAX_AMOUNT_MINOR + 1])
def test_amounts_outside_the_bound_are_rejected(amount: int) -> None:
    with pytest.raises(BelunoError) as error:
        check_amount(amount, field="amount_minor")
    assert code(error) == "AMOUNT_OUT_OF_RANGE"
    with pytest.raises(BelunoError):
        resolve_split(amount, SplitSpec(SplitMethod.EQUAL, entries(1)))


def test_payers_must_be_unique_positive_and_cover_the_amount() -> None:
    assert validate_payers(1000, [Payer(PEOPLE[0], 600), Payer(None, 400)])
    for payers in (
        [],
        [Payer(PEOPLE[0], 500), Payer(PEOPLE[0], 500)],
        [Payer(PEOPLE[0], 1000), Payer(PEOPLE[1], 0)],
        [Payer(PEOPLE[0], 999)],
    ):
        with pytest.raises(BelunoError) as error:
            validate_payers(1000, payers)
        assert code(error) in {"SPLIT_INVALID", "AMOUNT_OUT_OF_RANGE"}


def test_conversion_respects_both_exponents_and_rounds_half_even() -> None:
    assert convert(1000, from_exponent=0, to_exponent=2, rate=Decimal("0.0067")) == 670
    assert convert(10_000, from_exponent=3, to_exponent=2, rate=Decimal("3.25")) == 3250
    assert convert(150, from_exponent=2, to_exponent=0, rate=Decimal(1)) == 2
    assert convert(250, from_exponent=2, to_exponent=0, rate=Decimal(1)) == 2
    assert convert(350, from_exponent=2, to_exponent=0, rate=Decimal(1)) == 4
    with pytest.raises(BelunoError) as error:
        convert(MAX_AMOUNT_MINOR, from_exponent=2, to_exponent=0, rate=Decimal(25_000))
    assert code(error) == "AMOUNT_OUT_OF_RANGE"


@pytest.mark.parametrize(
    "raw", ["0", "-1", "abc", "NaN", "Infinity", "1000000001", "0.0000000000001"]
)
def test_rates_are_positive_bounded_and_precise_to_twelve_decimals(raw: str) -> None:
    with pytest.raises(BelunoError) as error:
        parse_rate(raw)
    assert code(error) == "FX_RATE_INVALID"


def test_agreed_amounts_imply_a_twelve_decimal_rate() -> None:
    rate = implied_rate(1000, from_exponent=2, to_minor=1100, to_exponent=2)
    assert rate == Decimal("1.1")
    assert implied_rate(100, from_exponent=2, to_minor=26_000, to_exponent=0) == Decimal(26_000)
    assert implied_rate(300, from_exponent=2, to_minor=100, to_exponent=2) == Decimal(
        "0.333333333333"
    )


def test_expense_postings_are_paid_minus_owed() -> None:
    alice, bob = Party(PEOPLE[0]), Party(PEOPLE[1])
    postings = expense_postings(
        [Payer(PEOPLE[0], 900)], [Share(PEOPLE[0], 300), Share(PEOPLE[1], 600)]
    )
    assert postings == {alice: 600, bob: -600}
    # Somebody who paid exactly their share has no posting at all.
    assert expense_postings([Payer(PEOPLE[0], 500)], [Share(PEOPLE[0], 500)]) == {}
    fund_paid = expense_postings([Payer(None, 400)], [Share(PEOPLE[0], 400)])
    assert fund_paid == {FUND: 400, alice: -400}


def test_reversal_moves_merged_parties_to_their_survivor() -> None:
    placeholder, survivor, other = Party(PEOPLE[0]), Party(PEOPLE[1]), Party(PEOPLE[2])
    original = {placeholder: 500, survivor: -200, other: -300}
    assert reversal_postings(original) == {placeholder: -500, survivor: 200, other: 300}
    assert reversal_postings(original, {placeholder: survivor}) == {survivor: -300, other: 300}


def test_refunds_follow_owed_shares() -> None:
    shares = [Share(PEOPLE[0], 0), Share(PEOPLE[1], 600), Share(PEOPLE[2], 300)]
    allocation = refund_allocation(100, shares)
    assert owed(allocation) == [67, 33]
    postings = refund_postings(Party(PEOPLE[0]), allocation)
    assert postings == {Party(PEOPLE[0]): -100, Party(PEOPLE[1]): 67, Party(PEOPLE[2]): 33}


def test_transfers_improve_the_payer_and_lower_the_receiver() -> None:
    debtor, creditor = Party(PEOPLE[0]), Party(PEOPLE[1])
    assert transfer_postings(debtor, creditor, 250) == {debtor: 250, creditor: -250}
    assert transfer_postings(debtor, FUND, 100) == {FUND: -100, debtor: 100}
    with pytest.raises(ValueError):
        transfer_postings(debtor, debtor, 1)


def test_debt_preview_is_deterministic_and_pays_out_the_fund() -> None:
    a, b, c, d = PEOPLE[:4]
    preview = simplify_debts({a: 500, b: -300, c: -300, d: 300}, fund_available=200)
    assert [
        (t.from_participant_id, t.to_participant_id, t.amount_minor) for t in preview.transfers
    ] == [
        (b, a, 300),
        (c, d, 300),
    ]
    assert [(p.to_participant_id, p.amount_minor) for p in preview.fund_payouts] == [(a, 200)]
    with pytest.raises(ValueError):
        simplify_debts({a: 1, b: -2})


def test_debt_preview_leaves_out_balances_within_the_tolerance() -> None:
    a, b, c = PEOPLE[:3]
    preview = simplify_debts({a: -103, b: 100, c: 3}, tolerance=5)
    assert [
        (t.from_participant_id, t.to_participant_id, t.amount_minor) for t in preview.transfers
    ] == [(a, b, 100)]
    assert simplify_debts({a: -3, b: 3}, tolerance=5).transfers == []
    payout = simplify_debts({a: 10, b: 2}, fund_available=12, tolerance=5)
    assert [(p.to_participant_id, p.amount_minor) for p in payout.fund_payouts] == [(a, 10)]


def test_ledger_status_and_commitment_tiers() -> None:
    assert next_ledger_status(LedgerStatus.OPEN, balances_zero=True, has_live_settlement=False) is (
        LedgerStatus.OPEN
    )
    assert next_ledger_status(LedgerStatus.OPEN, balances_zero=True, has_live_settlement=True) is (
        LedgerStatus.SETTLED
    )
    assert (
        next_ledger_status(LedgerStatus.SETTLED, balances_zero=False, has_live_settlement=True)
        is LedgerStatus.REOPENED
    )
    assert (
        next_ledger_status(LedgerStatus.REOPENED, balances_zero=True, has_live_settlement=True)
        is LedgerStatus.SETTLED
    )
    assert commitment_tier(CommitmentState.COMMITTED) is BudgetTier.COMMITTED
    assert commitment_tier(CommitmentState.ESTIMATED) is BudgetTier.ESTIMATED
    for state in (CommitmentState.CONVERTED, CommitmentState.CANCELLED, CommitmentState.REFUNDED):
        assert commitment_tier(state) is None


# --- properties --------------------------------------------------------------------

amounts = st.integers(min_value=1, max_value=MAX_AMOUNT_MINOR)
weights = st.lists(st.integers(min_value=1, max_value=1_000_000), min_size=1, max_size=100)


@given(st.integers(min_value=0, max_value=MAX_AMOUNT_MINOR), weights)
@settings(max_examples=300, deadline=None)
def test_largest_remainder_is_exact_and_within_one_unit_of_the_quota(
    total: int, shares: list[int]
) -> None:
    allocation = largest_remainder(total, shares)
    assert sum(allocation) == total
    weight_sum = sum(shares)
    for value, weight in zip(allocation, shares, strict=True):
        floor = total * weight // weight_sum
        assert floor <= value <= floor + 1
    assert allocation == largest_remainder(total, shares)


@given(amounts, st.integers(min_value=1, max_value=100), st.sampled_from(list(SplitMethod)))
@settings(max_examples=300, deadline=None)
def test_every_method_resolves_to_the_exact_amount(
    amount: int, people: int, method: SplitMethod
) -> None:
    if method is SplitMethod.EQUAL or method is SplitMethod.SHARES:
        spec = SplitSpec(method, entries(*range(1, people + 1)))
    elif method is SplitMethod.PERCENTAGE:
        points = largest_remainder(10_000, [1] * min(people, 100))
        points = [value for value in points if value > 0]
        spec = SplitSpec(method, entries(*points))
    elif method is SplitMethod.EXACT:
        spec = SplitSpec(method, entries(*largest_remainder(amount, [1] * people)))
    elif method is SplitMethod.ADJUSTMENT:
        spec = SplitSpec(method, entries(amount // 2, *([0] * (people - 1))))
    else:
        cut = largest_remainder(amount, [1] * min(people, 10))
        cut = [value for value in cut if value > 0]
        spec = SplitSpec(
            method,
            items=tuple(SplitItem(value, tuple(PEOPLE[i : i + 3])) for i, value in enumerate(cut)),
        )
    shares = resolve_split(amount, spec)
    assert sum(owed(shares)) == amount
    assert all(value >= 0 for value in owed(shares))
    assert len({share.participant_id for share in shares}) == len(shares)
    assert resolve_split(amount, spec) == shares


@given(amounts, st.integers(min_value=1, max_value=100))
@settings(max_examples=200, deadline=None)
def test_equal_splits_differ_by_at_most_one_unit(amount: int, people: int) -> None:
    values = owed(resolve_split(amount, SplitSpec(SplitMethod.EQUAL, entries(*([1] * people)))))
    assert max(values) - min(values) <= 1
    assert values == sorted(values, reverse=True)


@given(
    st.lists(st.integers(min_value=1, max_value=10**9), min_size=1, max_size=20),
    st.lists(st.integers(min_value=1, max_value=10**6), min_size=1, max_size=20),
    st.booleans(),
)
@settings(max_examples=300, deadline=None)
def test_expense_and_reversal_postings_cancel(
    paid: list[int], weights_: list[int], fund_pays: bool
) -> None:
    amount = sum(paid)
    payers = [Payer(None if fund_pays and i == 0 else PEOPLE[i], v) for i, v in enumerate(paid)]
    shares = resolve_split(amount, SplitSpec(SplitMethod.SHARES, entries(*weights_)))
    postings = expense_postings(payers, shares)
    assert sum(postings.values()) == 0 and all(postings.values())
    for party, value in postings.items():
        expected = sum(p.amount_minor for p in payers if Party(p.participant_id) == party) - sum(
            s.owed_minor for s in shares if Party(s.participant_id) == party
        )
        assert value == expected
    reversal = reversal_postings(postings)
    assert {party: postings[party] + reversal[party] for party in postings} == dict.fromkeys(
        postings, 0
    )


@given(
    st.lists(st.integers(min_value=0, max_value=10**9), min_size=1, max_size=30),
    st.integers(min_value=1, max_value=10**9),
)
@settings(max_examples=300, deadline=None)
def test_refund_allocation_never_exceeds_owed(owed_values: list[int], refund: int) -> None:
    shares = [Share(PEOPLE[i], value) for i, value in enumerate(owed_values)]
    total = sum(owed_values)
    if total == 0:
        return
    refund = min(refund, total)
    allocation = refund_allocation(refund, shares)
    assert sum(owed(allocation)) == refund
    by_person = {share.participant_id: share.owed_minor for share in shares}
    assert all(0 < share.owed_minor <= by_person[share.participant_id] for share in allocation)


@given(
    st.lists(st.integers(min_value=-(10**9), max_value=10**9), min_size=1, max_size=40),
    st.integers(min_value=0, max_value=10**9),
)
@settings(max_examples=300, deadline=None)
def test_debt_preview_zeroes_every_balance_with_few_transfers(values: list[int], fund: int) -> None:
    balances = {PEOPLE[i]: value for i, value in enumerate(values)}
    # Make the participants' total equal to what the fund holds.
    balances[PEOPLE[0]] += fund - sum(values)
    preview = simplify_debts(balances, fund_available=fund)
    remaining = dict(balances)
    for transfer in preview.transfers:
        assert transfer.amount_minor > 0
        remaining[transfer.from_participant_id] += transfer.amount_minor
        remaining[transfer.to_participant_id] -= transfer.amount_minor
    for payout in preview.fund_payouts:
        remaining[payout.to_participant_id] -= payout.amount_minor
    assert set(remaining.values()) <= {0}
    non_zero = sum(1 for value in balances.values() if value)
    assert len(preview.transfers) <= max(non_zero - 1, 0)
    assert sum(p.amount_minor for p in preview.fund_payouts) == fund
    assert simplify_debts(balances, fund_available=fund) == preview


@given(
    st.integers(min_value=1, max_value=10**10),
    st.integers(min_value=0, max_value=4),
    st.integers(min_value=0, max_value=4),
    st.decimals(min_value=Decimal("0.000001"), max_value=Decimal(1000), places=6),
)
@settings(max_examples=300, deadline=None)
def test_conversion_is_within_half_a_unit_of_the_exact_value(
    amount: int, from_exponent: int, to_exponent: int, rate: Decimal
) -> None:
    exact = Decimal(amount) * rate * Decimal(10) ** (to_exponent - from_exponent)
    if exact > MAX_AMOUNT_MINOR:
        return
    converted = convert(amount, from_exponent=from_exponent, to_exponent=to_exponent, rate=rate)
    assert abs(Decimal(converted) - exact) <= Decimal("0.5")
