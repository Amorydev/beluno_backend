"""Trips and hangouts, member settings, and per-member capabilities."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    join_with_invite,
)
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

KYOTO = {"name": "Kyoto", "code": "KYO", "country_code": "JP", "start_date": "2027-03-20"}


async def create(api: httpx.AsyncClient, user: SignedIn, **body: Any) -> httpx.Response:
    return await api.post("/v1/plans", json={"title": "Plan", **body}, headers=user.headers)


async def test_trips_take_destinations_and_hangouts_take_an_activity(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    trip = await create(
        api,
        owner,
        type="trip",
        base_currency="VND",
        destinations=[{"name": "Tokyo", "code": "TYO"}, KYOTO],
        expected_size=8,
    )
    assert trip.status_code == 201, trip.text
    body = trip.json()
    assert body["type"] == "trip" and body["activity"] is None
    assert [stop["name"] for stop in body["destinations"]] == ["Tokyo", "Kyoto"]
    assert body["destinations"][1] == {**KYOTO, "end_date": None}
    assert body["expected_size"] == 8
    assert body["pass_color"] in {
        "indigo",
        "plum",
        "sea",
        "forest",
        "rust",
        "slate",
        "wine",
        "moss",
    }

    hangout = await create(api, owner, type="hangout", activity="karaoke", base_currency="VND")
    assert hangout.status_code == 201
    assert (hangout.json()["activity"], hangout.json()["destinations"]) == ("karaoke", [])

    for body_overrides in (
        {"type": "trip", "activity": "dinner"},
        {"type": "hangout", "destinations": [KYOTO]},
        {
            "type": "trip",
            "destinations": [
                {"name": "Kyoto", "start_date": "2027-03-20", "end_date": "2027-03-19"}
            ],
        },
        {"type": "trip", "destinations": [{"name": f"City {index}"} for index in range(11)]},
        {"type": "trip", "pass_color": "neon"},
    ):
        refused = await create(api, owner, base_currency="VND", **body_overrides)
        assert refused.status_code == 422, body_overrides

    # A hangout never gains destinations later, and a trip keeps its type.
    later = await api.patch(
        f"/v1/plans/{hangout.json()['id']}",
        json={"destinations": [KYOTO]},
        headers=if_match(1, owner),
    )
    assert later.status_code == 422
    moved = await api.patch(
        f"/v1/plans/{trip.json()['id']}",
        json={"destinations": [], "pass_color": "plum", "expected_size": None},
        headers=if_match(1, owner),
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["destinations"], moved.json()["pass_color"]) == ([], "plum")
    assert moved.json()["expected_size"] is None


async def test_the_default_currency_fills_in_a_missing_base_currency(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    missing = await create(api, owner, type="hangout")
    assert missing.status_code == 422

    unknown = await api.patch(
        "/v1/me", json={"default_currency": "XYZ"}, headers=if_match(1, owner)
    )
    assert unknown.status_code == 422
    profile = await api.patch(
        "/v1/me", json={"default_currency": "VND"}, headers=if_match(1, owner)
    )
    assert profile.status_code == 200 and profile.json()["default_currency"] == "VND"
    hangout = await create(api, owner, type="hangout", activity="dinner")
    assert hangout.status_code == 201 and hangout.json()["base_currency"] == "VND"
    explicit = await create(api, owner, type="trip", base_currency="JPY")
    assert explicit.json()["base_currency"] == "JPY"


async def test_members_pick_their_colour_and_managers_set_shares(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    friend = await sign_in(api, identity_provider, name="Minh")
    plan = (await create(api, owner, type="trip", base_currency="VND")).json()
    assert plan["my_participant"]["avatar_color"] == "blue"
    assert plan["my_participant"]["default_share"] == 100
    invite = await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    joined = await api.post(
        "/v1/invites/redeem",
        json={"token": invite.json()["token"], "avatar_color": "rose"},
        headers=friend.headers,
    )
    minh = joined.json()["participant"]
    assert (minh["avatar_color"], minh["capabilities"]) == ("rose", [])
    path = f"/v1/plans/{plan['id']}/participants/{minh['id']}"

    recoloured = await api.patch(path, json={"avatar_color": "olive"}, headers=if_match(1, friend))
    assert recoloured.status_code == 200 and recoloured.json()["avatar_color"] == "olive"
    own_share = await api.patch(path, json={"default_share": 200}, headers=if_match(2, friend))
    assert own_share.status_code == 403
    owner_row = plan["my_participant"]
    others = await api.patch(
        f"/v1/plans/{plan['id']}/participants/{owner_row['id']}",
        json={"avatar_color": "teal"},
        headers=if_match(1, friend),
    )
    assert others.status_code == 403

    doubled = await api.patch(path, json={"default_share": 200}, headers=if_match(2, owner))
    assert doubled.status_code == 200 and doubled.json()["default_share"] == 200
    own = await api.patch(
        f"/v1/plans/{plan['id']}/participants/{owner_row['id']}",
        json={"default_share": 150},
        headers=if_match(1, owner),
    )
    assert own.status_code == 200 and own.json()["default_share"] == 150
    empty = await api.patch(path, json={}, headers=if_match(3, owner))
    assert empty.status_code == 422


async def test_capabilities_let_a_member_manage_expenses_and_budgets(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    trip: FinancePlan = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))
    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    bea_user, dan_user = trip.members["Bea"], trip.members["Dan"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(900, ann, [ann, bea, cam]))
    void_path = trip.path(f"/expenses/{expense['id']}/void")
    budget_body = {"scope": "total", "limit_minor": 50_000}

    assert (await api.post(void_path, headers=if_match(1, bea_user))).status_code == 403
    assert (
        await api.post(trip.path("/budgets"), json=budget_body, headers=bea_user.headers)
    ).status_code == 403

    granted = await api.patch(
        trip.path(f"/participants/{bea}"),
        json={"capabilities": ["expenses.manage", "budgets.manage"]},
        headers=if_match(1, trip.owner),
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["capabilities"] == ["budgets.manage", "expenses.manage"]
    assert (await api.post(void_path, headers=if_match(1, bea_user))).status_code == 200
    budget = await api.post(trip.path("/budgets"), json=budget_body, headers=bea_user.headers)
    assert budget.status_code == 201, budget.text

    # Viewers, guests, and placeholders cannot be granted management.
    dan = trip.people["Dan"]
    viewer = await api.patch(
        trip.path(f"/participants/{dan}"), json={"role": "viewer"}, headers=if_match(1, trip.owner)
    )
    assert viewer.status_code == 200
    refused = await api.patch(
        trip.path(f"/participants/{dan}"),
        json={"capabilities": ["expenses.manage"]},
        headers=if_match(2, trip.owner),
    )
    assert refused.status_code == 422
    placeholder = await api.patch(
        trip.path(f"/participants/{cam}"),
        json={"capabilities": ["budgets.manage"]},
        headers=if_match(1, trip.owner),
    )
    assert placeholder.status_code == 422
    # A member cannot hand themselves capabilities.
    dan_self = await api.patch(
        trip.path(f"/participants/{dan}"),
        json={"capabilities": ["budgets.manage"]},
        headers=if_match(2, dan_user),
    )
    assert dan_self.status_code == 403


async def test_insiders_cannot_grant_themselves_capabilities(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, live_settings: Settings
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    member = await sign_in(api, identity_provider, name="Minh")
    plan = (await create(api, owner, type="trip", base_currency="VND")).json()
    row = await join_with_invite(api, owner, plan["id"], member)
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection:
        for statement in (
            "UPDATE plans.plan_participants SET capabilities = '{expenses.manage}' WHERE id = %s",
            "UPDATE plans.plan_participants SET default_share = 900 WHERE id = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (member.user_id,))
                connection.execute(statement, (row["id"],))
        with connection.transaction():
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (member.user_id,))
            updated = connection.execute(
                "UPDATE plans.plan_participants SET avatar_color = 'olive' WHERE id = %s",
                (row["id"],),
            )
            assert updated.rowcount == 1


async def test_admins_manage_members_but_not_the_owner_or_other_admins(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    first, second, member = [
        await sign_in(api, identity_provider, name=name) for name in ("An", "Bao", "Minh")
    ]
    plan = (await create(api, owner, type="trip", base_currency="VND")).json()
    rows = {
        person.user_id: await join_with_invite(api, owner, plan["id"], person)
        for person in (first, second, member)
    }
    for admin in (first, second):
        promoted = await api.patch(
            f"/v1/plans/{plan['id']}/participants/{rows[admin.user_id]['id']}",
            json={"role": "admin"},
            headers=if_match(1, owner),
        )
        assert promoted.status_code == 200, promoted.text

    def path(row_id: str) -> str:
        return f"/v1/plans/{plan['id']}/participants/{row_id}"

    owner_row, other_admin = plan["my_participant"]["id"], rows[second.user_id]["id"]
    for row_id, version, body in (
        (owner_row, 1, {"default_share": 10_000}),
        (owner_row, 1, {"avatar_color": "olive"}),
        (other_admin, 2, {"default_share": 1}),
        (other_admin, 2, {"capabilities": []}),
    ):
        refused = await api.patch(path(row_id), json=body, headers=if_match(version, first))
        assert refused.status_code == 403, (row_id, body)
    managed = await api.patch(
        path(rows[member.user_id]["id"]),
        json={"default_share": 50, "capabilities": ["expenses.manage"]},
        headers=if_match(1, first),
    )
    assert managed.status_code == 200, managed.text
    nulls = await api.patch(
        path(rows[member.user_id]["id"]), json={"role": None}, headers=if_match(2, first)
    )
    assert nulls.status_code == 422


async def test_capabilities_end_with_the_member_role_and_membership(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    bea, dan, cam = [await sign_in(api, identity_provider, name=n) for n in ("Bea", "Dan", "Cam")]
    plan = (await create(api, owner, type="trip", base_currency="VND")).json()
    rows = {p.user_id: await join_with_invite(api, owner, plan["id"], p) for p in (bea, dan, cam)}
    grant = {"capabilities": ["budgets.manage", "expenses.manage"]}

    async def patch(person: SignedIn, version: int, body: dict[str, Any]) -> dict[str, Any]:
        response = await api.patch(
            f"/v1/plans/{plan['id']}/participants/{rows[person.user_id]['id']}",
            json=body,
            headers=if_match(version, owner),
        )
        assert response.status_code == 200, response.text
        row: dict[str, Any] = response.json()
        return row

    async def capabilities_of(person: SignedIn) -> list[str]:
        roster = await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)
        return next(p["capabilities"] for p in roster.json() if p["user_id"] == person.user_id)

    # A demotion drops the grants; promoting back does not restore them.
    await patch(bea, 1, grant)
    assert (await patch(bea, 2, {"role": "viewer"}))["capabilities"] == []
    assert (await patch(bea, 3, {"role": "member"}))["capabilities"] == []

    # Leaving or being removed ends them too, so rejoining starts clean.
    await patch(bea, 4, grant)
    assert (await api.post(f"/v1/plans/{plan['id']}/leave", headers=bea.headers)).status_code in (
        200,
        204,
    )
    await join_with_invite(api, owner, plan["id"], bea)
    assert await capabilities_of(bea) == []
    await patch(dan, 1, grant)
    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{rows[dan.user_id]['id']}", headers=owner.headers
    )
    assert removed.status_code == 204, removed.text
    readded = await api.post(
        f"/v1/plans/{plan['id']}/participants", json={"user_id": dan.user_id}, headers=owner.headers
    )
    assert readded.status_code == 201, readded.text
    assert readded.json()["capabilities"] == []

    # A new owner holds everything through the role, not through grants.
    await patch(cam, 1, grant)
    transferred = await api.post(
        f"/v1/plans/{plan['id']}/ownership-transfer",
        json={"new_owner_participant_id": rows[cam.user_id]["id"]},
        headers=if_match(1, owner),
    )
    assert transferred.status_code == 200, transferred.text
    assert await capabilities_of(cam) == []


async def test_insiders_cannot_change_a_plan_type_or_grant_non_members(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, live_settings: Settings
) -> None:
    owner = await sign_in(api, identity_provider, name="Linh")
    viewer = await sign_in(api, identity_provider, name="Minh")
    plan = (await create(api, owner, type="trip", base_currency="VND")).json()
    row = await join_with_invite(api, owner, plan["id"], viewer)
    demoted = await api.patch(
        f"/v1/plans/{plan['id']}/participants/{row['id']}",
        json={"role": "viewer"},
        headers=if_match(1, owner),
    )
    assert demoted.status_code == 200
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection:
        for statement, params, error in (
            (
                "UPDATE plans.plans SET type = 'hangout' WHERE id = %s",
                (plan["id"],),
                psycopg.errors.InsufficientPrivilege,
            ),
            (
                "UPDATE plans.plan_participants SET capabilities = '{expenses.manage}' "
                "WHERE id = %s",
                (row["id"],),
                psycopg.errors.CheckViolation,
            ),
        ):
            with pytest.raises(error), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (owner.user_id,))
                connection.execute(statement, params)
