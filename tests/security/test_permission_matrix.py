"""The documented permission matrix is the policy: every cell is checked, and
every combination of relationship and state is swept for invariant violations."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from beluno.authorization.policy import (
    PLAN_RULES,
    AccessState,
    Capability,
    Decision,
    PlanAction,
    PlanRole,
    PlanState,
    PlanSubject,
    can_manage_participant,
    decide_plan,
)

MATRIX_DOC = Path(__file__).parents[2] / "docs" / "contracts" / "permission-matrix.md"


def parse_matrix(name: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    text = MATRIX_DOC.read_text(encoding="utf-8")
    block = re.search(rf"<!-- {name}:start -->(.*?)<!-- {name}:end -->", text, re.S)
    assert block is not None, f"{name} table missing from {MATRIX_DOC}"
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in block.group(1).strip().splitlines()
    ]
    header, body = rows[0], rows[2:]
    columns = header[1:]
    return columns, {row[0]: dict(zip(columns, row[1:], strict=True)) for row in body}


PLAN_COLUMNS, PLAN_MATRIX = parse_matrix("plan-matrix")


def test_matrix_documents_every_action() -> None:
    assert set(PLAN_MATRIX) == {action.value for action in PlanAction}


def plan_subject(column: str, *, step_up_fresh: bool) -> PlanSubject:
    participant = column != "outsider"
    return PlanSubject(
        participant_role=PlanRole(column) if participant else None,
        access_state=AccessState.ACTIVE if participant else None,
        plan_state=PlanState.PLANNING,
        deletion_scheduled=False,
        actor_is_guest=column == "guest",
        step_up_fresh=step_up_fresh,
    )


@pytest.mark.parametrize(
    ("action", "column"),
    [(action, column) for action in PLAN_MATRIX for column in PLAN_COLUMNS],
)
def test_plan_matrix_cell(action: str, column: str) -> None:
    expected = PLAN_MATRIX[action][column]
    fresh = decide_plan(PlanAction(action), plan_subject(column, step_up_fresh=True))
    stale = decide_plan(PlanAction(action), plan_subject(column, step_up_fresh=False))
    if expected == "allow":
        assert (fresh, stale) == (Decision.ALLOW, Decision.ALLOW)
    elif expected == "step-up":
        assert (fresh, stale) == (Decision.ALLOW, Decision.STEP_UP_REQUIRED)
    else:
        assert expected == "deny"
        assert fresh in (Decision.FORBIDDEN, Decision.HIDDEN)


PLAN_SWEEP = list(
    itertools.product(
        [None, *PlanRole],
        [None, *AccessState],
        list(PlanState),
        [False, True],  # deletion scheduled
        [False, True],  # actor is guest
        [False, True],  # step-up fresh
    )
)


def test_plan_sweep_never_violates_invariants() -> None:
    checked = 0
    for role, state, plan_state, deleting, guest, fresh in PLAN_SWEEP:
        subject = PlanSubject(
            participant_role=role,
            access_state=state,
            plan_state=plan_state,
            deletion_scheduled=deleting,
            actor_is_guest=guest,
            step_up_fresh=fresh,
        )
        participant = state is AccessState.ACTIVE and role is not None
        for action in PlanAction:
            decision = decide_plan(action, subject)
            checked += 1
            if not participant:
                assert decision is Decision.HIDDEN, (action, subject)
                continue
            if decision is not Decision.ALLOW:
                continue
            rule = PLAN_RULES[action]
            assert role in rule.roles, (action, subject)
            assert not (rule.registered_only and guest), (action, subject)
            assert not (deleting and not rule.allowed_during_deletion), (action, subject)
            assert rule.plan_states is None or plan_state in rule.plan_states
            assert not (rule.step_up and not fresh)
    assert checked == len(PLAN_SWEEP) * len(PlanAction)


def test_participant_target_rules() -> None:
    owner, admin, member = PlanRole.OWNER, PlanRole.ADMIN, PlanRole.MEMBER
    assert can_manage_participant(owner, PlanRole.ADMIN, new_role=PlanRole.VIEWER)
    assert not can_manage_participant(owner, PlanRole.OWNER)
    assert not can_manage_participant(owner, member, new_role=PlanRole.OWNER)
    assert not can_manage_participant(owner, PlanRole.GUEST, new_role=PlanRole.MEMBER)
    assert can_manage_participant(admin, PlanRole.GUEST)
    assert not can_manage_participant(admin, PlanRole.ADMIN)
    assert not can_manage_participant(admin, member, new_role=PlanRole.ADMIN)
    assert not can_manage_participant(member, PlanRole.VIEWER)


@pytest.mark.parametrize("role", list(PlanRole))
def test_capabilities_only_extend_plain_members(role: PlanRole) -> None:
    def subject(capabilities: frozenset[Capability]) -> PlanSubject:
        return PlanSubject(
            participant_role=role,
            access_state=AccessState.ACTIVE,
            plan_state=PlanState.PLANNING,
            deletion_scheduled=False,
            actor_is_guest=role is PlanRole.GUEST,
            step_up_fresh=True,
            capabilities=capabilities,
        )

    for action, capability in (
        (PlanAction.MANAGE_EXPENSES, Capability.MANAGE_EXPENSES),
        (PlanAction.MANAGE_BUDGETS, Capability.MANAGE_BUDGETS),
    ):
        without = decide_plan(action, subject(frozenset()))
        granted = decide_plan(action, subject(frozenset({capability})))
        other = decide_plan(action, subject(frozenset(Capability) - {capability}))
        manager = role in (PlanRole.OWNER, PlanRole.ADMIN)
        assert (without is Decision.ALLOW) == manager
        assert (granted is Decision.ALLOW) == (manager or role is PlanRole.MEMBER)
        assert other == without
    # Capabilities never unlock anything beyond their own action.
    every = frozenset(Capability)
    for action in PlanAction:
        if action in (PlanAction.MANAGE_EXPENSES, PlanAction.MANAGE_BUDGETS):
            continue
        assert decide_plan(action, subject(every)) == decide_plan(action, subject(frozenset()))
