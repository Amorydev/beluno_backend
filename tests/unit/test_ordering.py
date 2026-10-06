"""Fractional ordering keys: always insertable, always sortable, deterministic rebalance."""

from __future__ import annotations

import random
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from beluno.sync.ordering import (
    ALPHABET,
    OrderingKeyError,
    first_key,
    is_valid_key,
    key_between,
    rebalance,
)

valid_keys = st.text(alphabet=ALPHABET, min_size=1, max_size=12).filter(is_valid_key)


@given(st.lists(st.integers(0, 10_000), min_size=1, max_size=300), st.integers())
@settings(max_examples=200, deadline=None)
def test_random_insertions_keep_a_strict_order(positions: list[int], seed: int) -> None:
    rng = random.Random(seed)
    keys: list[str] = []
    for position in positions:
        index = position % (len(keys) + 1) if rng.random() < 0.7 else len(keys)
        lower = keys[index - 1] if index > 0 else None
        upper = keys[index] if index < len(keys) else None
        key = key_between(lower, upper)
        assert is_valid_key(key)
        assert lower is None or lower < key
        assert upper is None or key < upper
        keys.insert(index, key)
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


@given(valid_keys, valid_keys)
@settings(max_examples=300, deadline=None)
def test_between_any_two_distinct_keys(a: str, b: str) -> None:
    if a == b:
        with pytest.raises(OrderingKeyError):
            key_between(a, b)
        return
    lower, upper = sorted((a, b))
    key = key_between(lower, upper)
    assert lower < key < upper and is_valid_key(key)


@given(valid_keys)
@settings(max_examples=100, deadline=None)
def test_open_ends(key: str) -> None:
    assert key_between(None, key) < key
    assert key_between(key, None) > key
    assert is_valid_key(key_between(None, key)) and is_valid_key(key_between(key, None))


@given(st.integers(0, 5_000))
@settings(max_examples=60, deadline=None)
def test_rebalance_is_sorted_short_and_deterministic(count: int) -> None:
    keys = rebalance(count)
    assert len(keys) == count
    assert keys == sorted(keys) and len(set(keys)) == count
    assert all(is_valid_key(key) and len(key) <= 3 for key in keys)
    assert rebalance(count) == keys
    for lower, upper in pairwise(keys):
        assert lower < key_between(lower, upper) < upper


def test_invalid_keys_and_edge_cases() -> None:
    assert first_key() == "V"
    assert key_between("A", "A01") == "A00V"
    for bad in ("", "A0", "a!", "0", "x" * 129):
        assert not is_valid_key(bad)
        with pytest.raises(OrderingKeyError):
            key_between(bad, None)
    with pytest.raises(OrderingKeyError):
        key_between("B", "A")
    with pytest.raises(OrderingKeyError):
        rebalance(-1)
    assert rebalance(0) == []
    assert rebalance(1) == ["V"]


def test_repeated_front_insertions_stay_bounded_by_length_growth() -> None:
    key: str | None = None
    for _ in range(200):
        key = key_between(None, key)
    assert key is not None and len(key) < 60
