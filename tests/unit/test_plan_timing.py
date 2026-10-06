from __future__ import annotations

from datetime import UTC, datetime

import pytest

from beluno.authorization.policy import PlanState
from beluno.contracts.errors import BelunoError
from beluno.modules.plans.timing import check_transition, resolve_local


def test_dst_gap_and_overlap_resolution() -> None:
    gap_local = datetime(2026, 3, 8, 2, 30)
    with pytest.raises(BelunoError):
        resolve_local(gap_local, "America/New_York")
    overlap = resolve_local(datetime(2026, 11, 1, 1, 30), "America/New_York")
    assert overlap == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        resolve_local(datetime(2026, 1, 1, tzinfo=UTC), "UTC")


def test_plan_lifecycle_transitions() -> None:
    check_transition(PlanState.PLANNING, PlanState.ACTIVE)
    check_transition(PlanState.CANCELLED, PlanState.PLANNING)
    with pytest.raises(BelunoError) as captured:
        check_transition(PlanState.PLANNING, PlanState.ARCHIVED)
    assert captured.value.code == "INVALID_STATE_TRANSITION"
