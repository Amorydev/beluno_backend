"""The documented permission matrix is the policy: every cell is checked, and
every combination of relationship and state is swept for invariant violations."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from beluno.authorization.policy import (
    GROUP_RULES,
    GROUP_VISIBLE_READ_ACTIONS,
    PLAN_RULES,
    AccessState,
    Decision,
    GroupAction,
    GroupRole,
    GroupState,
    GroupSubject,
    MembershipState,
    PlanAction,
    PlanRole,
    PlanState,
    PlanSubject,
    Visibility,
    can_manage_group_member,
    can_manage_participant,
    decide_group,
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
GROUP_COLUMNS, GROUP_MATRIX = parse_matrix("group-matrix")


def test_matrix_documents_every_action() -> None:
    assert set(PLAN_MATRIX) == {action.value for action in PlanAction}
    assert set(GROUP_MATRIX) == {action.value for action in GroupAction}


def plan_subject(column: str, *, step_up_fresh: bool) -> PlanSubject:
    participant = column != "group-member"
    return PlanSubject(
        participant_role=PlanRole(column) if participant else None,
        access_state=AccessState.ACTIVE if participant else None,
        group_member_active=not participant,
        visibility=Visibility.GROUP,
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


def group_subject(column: str, *, step_up_fresh: bool) -> GroupSubject:
    invited = column == "invited"
    return GroupSubject(
        role=GroupRole.MEMBER if invited else GroupRole(column),
        membership_state=MembershipState.INVITED if invited else MembershipState.ACTIVE,
        group_state=GroupState.ACTIVE,
        actor_is_guest=False,
        step_up_fresh=step_up_fresh,
    )


@pytest.mark.parametrize(
    ("action", "column"),
    [(action, column) for action in GROUP_MATRIX for column in GROUP_COLUMNS],
)
def test_group_matrix_cell(action: str, column: str) -> None:
    expected = GROUP_MATRIX[action][column]
    fresh = decide_group(GroupAction(action), group_subject(column, step_up_fresh=True))
    stale = decide_group(GroupAction(action), group_subject(column, step_up_fresh=False))
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
        list(Visibility),
        [False, True],  # group member active
        [False, True],  # deletion scheduled
        [False, True],  # actor is guest
        [False, True],  # step-up fresh
    )
)


def test_plan_sweep_never_violates_invariants() -> None:
    checked = 0
    for role, state, plan_state, visibility, in_group, deleting, guest, fresh in PLAN_SWEEP:
        subject = PlanSubject(
            participant_role=role,
            access_state=state,
            group_member_active=in_group,
            visibility=visibility,
            plan_state=plan_state,
            deletion_scheduled=deleting,
            actor_is_guest=guest,
            step_up_fresh=fresh,
        )
        participant = state is AccessState.ACTIVE and role is not None
        group_reader = visibility is Visibility.GROUP and in_group
        for action in PlanAction:
            decision = decide_plan(action, subject)
            checked += 1
            if not participant and not group_reader:
                assert decision is Decision.HIDDEN, (action, subject)
                continue
            if decision is not Decision.ALLOW:
                continue
            if not participant:
                assert action in GROUP_VISIBLE_READ_ACTIONS or action is PlanAction.JOIN
                assert not (action is PlanAction.JOIN and (guest or deleting))
                continue
            rule = PLAN_RULES[action]
            assert role in rule.roles, (action, subject)
            assert not (rule.registered_only and guest), (action, subject)
            assert not (deleting and not rule.allowed_during_deletion), (action, subject)
            assert rule.plan_states is None or plan_state in rule.plan_states
            assert not (rule.step_up and not fresh)
    assert checked == len(PLAN_SWEEP) * len(PlanAction)


def test_group_sweep_never_violates_invariants() -> None:
    for role, state, group_state, guest, fresh in itertools.product(
        [None, *GroupRole],
        [None, *MembershipState],
        list(GroupState),
        [False, True],
        [False, True],
    ):
        subject = GroupSubject(
            role=role,
            membership_state=state,
            group_state=group_state,
            actor_is_guest=guest,
            step_up_fresh=fresh,
        )
        for action in GroupAction:
            decision = decide_group(action, subject)
            if state in (None, MembershipState.LEFT, MembershipState.REMOVED) or (
                state is MembershipState.ACTIVE and role is None
            ):
                assert decision is Decision.HIDDEN
            elif state is MembershipState.INVITED:
                assert (decision is Decision.ALLOW) == (
                    action in (GroupAction.VIEW, GroupAction.RESPOND_INVITATION)
                )
            elif decision is Decision.ALLOW and action is not GroupAction.RESPOND_INVITATION:
                rule = GROUP_RULES[action]
                assert role in rule.roles
                assert not (rule.step_up and not fresh)
                assert not (
                    group_state is GroupState.DELETION_SCHEDULED
                    and not rule.allowed_during_deletion
                )


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


def test_group_member_target_rules() -> None:
    owner, admin, member = GroupRole.OWNER, GroupRole.ADMIN, GroupRole.MEMBER
    assert can_manage_group_member(owner, admin, new_role=member)
    assert not can_manage_group_member(owner, owner)
    assert not can_manage_group_member(owner, member, new_role=owner)
    assert can_manage_group_member(admin, member)
    assert not can_manage_group_member(admin, admin)
    assert not can_manage_group_member(admin, member, new_role=admin)
    assert not can_manage_group_member(member, member)
