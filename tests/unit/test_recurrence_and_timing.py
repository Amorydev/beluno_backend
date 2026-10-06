from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from beluno.authorization.policy import PlanState
from beluno.contracts.errors import BelunoError
from beluno.modules.plans.recurrence import (
    continuation_rule,
    end_before,
    normalize_rule,
    occurrence_dates,
)
from beluno.modules.plans.timing import GapPolicy, check_transition, resolve_local


def test_rules_are_normalized_to_a_stable_form() -> None:
    assert normalize_rule("rrule:byday=th;freq=weekly") == "FREQ=WEEKLY;BYDAY=TH"
    assert normalize_rule("FREQ=MONTHLY;COUNT=6;BYDAY=-1FR") == "FREQ=MONTHLY;BYDAY=-1FR;COUNT=6"


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=HOURLY",
        "FREQ=WEEKLY;BYHOUR=9",
        "FREQ=WEEKLY;COUNT=3;UNTIL=20270101",
        "FREQ=WEEKLY;INTERVAL=0",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=WEEKLY;UNTIL=20270231",
        "FREQ=WEEKLY;FREQ=DAILY",
        "",
    ],
)
def test_unsupported_rules_are_rejected(rule: str) -> None:
    with pytest.raises(BelunoError) as captured:
        normalize_rule(rule)
    assert captured.value.status == 422


def test_occurrences_are_bounded_by_window_and_limit() -> None:
    dates = occurrence_dates(
        "FREQ=MONTHLY;BYDAY=-1FR",
        start_date=date(2026, 1, 1),
        window_start=date(2026, 3, 1),
        window_end=date(2026, 6, 30),
    )
    assert dates == [date(2026, 3, 27), date(2026, 4, 24), date(2026, 5, 29), date(2026, 6, 26)]
    capped = occurrence_dates(
        "FREQ=DAILY",
        start_date=date(2026, 1, 1),
        window_start=date(2026, 1, 1),
        window_end=date(2026, 12, 31),
        limit=5,
    )
    assert len(capped) == 5


def test_weekly_wall_clock_time_survives_dst_change() -> None:
    dates = occurrence_dates(
        "FREQ=WEEKLY;BYDAY=TH",
        start_date=date(2026, 10, 1),
        window_start=date(2026, 10, 22),
        window_end=date(2026, 11, 5),
    )
    instants = [
        resolve_local(
            datetime.combine(day, time(19, 0)), "America/New_York", on_gap=GapPolicy.SHIFT_FORWARD
        )
        for day in dates
    ]
    # EDT (UTC-4) before 1 Nov 2026, EST (UTC-5) after: same 19:00 local, different UTC.
    assert [instant.strftime("%m-%d %H:%M") for instant in instants] == [
        "10-22 23:00",
        "10-29 23:00",
        "11-06 00:00",
    ]


def test_dst_gap_and_overlap_resolution() -> None:
    gap_local = datetime(2026, 3, 8, 2, 30)
    with pytest.raises(BelunoError):
        resolve_local(gap_local, "America/New_York", on_gap=GapPolicy.REJECT)
    shifted = resolve_local(gap_local, "America/New_York", on_gap=GapPolicy.SHIFT_FORWARD)
    assert shifted == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    overlap = resolve_local(
        datetime(2026, 11, 1, 1, 30), "America/New_York", on_gap=GapPolicy.REJECT
    )
    assert overlap == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        resolve_local(datetime(2026, 1, 1, tzinfo=UTC), "UTC", on_gap=GapPolicy.REJECT)


def test_split_rules_end_and_continue_without_extra_occurrences() -> None:
    rule = "FREQ=WEEKLY;BYDAY=MO;COUNT=10"
    assert end_before(rule, date(2026, 2, 2)) == "FREQ=WEEKLY;BYDAY=MO;UNTIL=20260201"
    # Mondays from 5 Jan: 5, 12, 19, 26 Jan happen before the 2 Feb boundary.
    assert continuation_rule(rule, start_date=date(2026, 1, 5), boundary=date(2026, 2, 2)) == (
        "FREQ=WEEKLY;BYDAY=MO;COUNT=6"
    )
    assert continuation_rule(
        "FREQ=DAILY", start_date=date(2026, 1, 1), boundary=date(2026, 2, 1)
    ) == ("FREQ=DAILY")
    with pytest.raises(BelunoError):
        continuation_rule(
            "FREQ=DAILY;COUNT=2", start_date=date(2026, 1, 1), boundary=date(2026, 3, 1)
        )


def test_plan_lifecycle_transitions() -> None:
    check_transition(PlanState.PLANNING, PlanState.ACTIVE)
    check_transition(PlanState.CANCELLED, PlanState.PLANNING)
    with pytest.raises(BelunoError) as captured:
        check_transition(PlanState.PLANNING, PlanState.ARCHIVED)
    assert captured.value.code == "INVALID_STATE_TRANSITION"
