"""Recurring series and allowlisted plan duplication on real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.plans.series import extend_all_series
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration

NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
async def worker_runtime(live_settings: Settings) -> AsyncIterator[Runtime]:
    database = Database.for_worker(live_settings)
    yield Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    await database.close()


async def group_with_member(api: httpx.AsyncClient, owner: SignedIn, member: SignedIn) -> dict:
    group = (
        await api.post(
            "/v1/groups",
            json={"name": "Football", "default_currency": "USD", "default_timezone": "UTC"},
            headers=owner.headers,
        )
    ).json()
    token = (
        await api.post(f"/v1/groups/{group['id']}/invites", json={}, headers=owner.headers)
    ).json()
    await api.post("/v1/invites/redeem", json={"token": token["token"]}, headers=member.headers)
    return group


def next_weekday(weekday: int) -> date:
    today = datetime.now(NEW_YORK).date()
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


async def create_weekly_series(
    api: httpx.AsyncClient, owner: SignedIn, group: dict, member: SignedIn
) -> dict:
    response = await api.post(
        "/v1/plan-series",
        json={
            "group_id": group["id"],
            "title": "Thursday football",
            "kind": "sport",
            "timezone": "America/New_York",
            "start_date": next_weekday(3).isoformat(),
            "local_start_time": "19:00:00",
            "duration_minutes": 90,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "participant_user_ids": [member.user_id],
            "horizon_days": 56,
        },
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_series_materializes_independent_occurrences_idempotently(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    worker_runtime: Runtime,
) -> None:
    owner = await sign_in(api, identity_provider)
    member = await sign_in(api, identity_provider, name="Striker")
    group = await group_with_member(api, owner, member)
    created = await create_weekly_series(api, owner, group, member)

    occurrences = created["occurrences"]
    assert 7 <= len(occurrences) <= 9
    for occurrence in occurrences:
        starts = datetime.fromisoformat(occurrence["timing"]["starts_at"]).astimezone(NEW_YORK)
        assert (starts.weekday(), starts.hour, starts.minute) == (3, 19, 0)
        assert occurrence["occurrence_key"] == starts.date().isoformat()
    roster = (
        await api.get(f"/v1/plans/{occurrences[0]['id']}/participants", headers=member.headers)
    ).json()
    assert {person["display_name"] for person in roster} == {"Test Member", "Striker"}

    # Worker retries and overlapping runs create nothing new.
    assert await extend_all_series(worker_runtime) == 0
    assert admin.scalar(
        "SELECT count(*) FROM plans.plans WHERE series_id = %s", created["series"]["id"]
    ) == len(occurrences)
    audit_actor = admin.scalar(
        "SELECT count(DISTINCT actor_user_id) FROM sync_audit.audit_events "
        "WHERE action = 'plan.created' AND plan_id IN "
        "(SELECT id FROM plans.plans WHERE series_id = %s)",
        created["series"]["id"],
    )
    assert audit_actor == 1


async def test_cancelling_one_occurrence_and_splitting_the_future(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    worker_runtime: Runtime,
) -> None:
    owner = await sign_in(api, identity_provider)
    member = await sign_in(api, identity_provider)
    group = await group_with_member(api, owner, member)
    created = await create_weekly_series(api, owner, group, member)
    series = created["series"]
    first, second, third, fourth = created["occurrences"][:4]

    cancelled = await api.post(
        f"/v1/plans/{second['id']}/state",
        json={"state": "cancelled"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert cancelled.json()["is_series_exception"] is True
    edited = await api.patch(
        f"/v1/plans/{fourth['id']}",
        json={"title": "Derby night"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert edited.status_code == 200
    assert await extend_all_series(worker_runtime) == 0

    forbidden = await api.post(
        f"/v1/plan-series/{series['id']}/split",
        json={"from_date": third["occurrence_key"], "title": "x"},
        headers={**member.headers, "If-Match": '"1"'},
    )
    assert forbidden.status_code == 403
    split = await api.post(
        f"/v1/plan-series/{series['id']}/split",
        json={"from_date": third["occurrence_key"], "title": "Football 7-a-side"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert split.status_code == 200, split.text
    successor = split.json()
    assert successor["series"]["id"] != series["id"]
    assert successor["series"]["start_date"] == third["occurrence_key"]
    assert {item["title"] for item in successor["occurrences"]} == {"Football 7-a-side"}

    old = (await api.get(f"/v1/plan-series/{series['id']}", headers=owner.headers)).json()
    states = {item["occurrence_key"]: (item["state"], item["title"]) for item in old["occurrences"]}
    assert states[first["occurrence_key"]] == ("planning", "Thursday football")
    assert states[second["occurrence_key"]] == ("cancelled", "Thursday football")
    assert states[third["occurrence_key"]] == ("cancelled", "Thursday football")
    assert states[fourth["occurrence_key"]] == ("planning", "Derby night")
    assert old["series"]["recurrence_rule"].endswith(
        "UNTIL="
        + (date.fromisoformat(third["occurrence_key"]) - timedelta(days=1)).strftime("%Y%m%d")
    )

    stopped = await api.post(
        f"/v1/plan-series/{successor['series']['id']}/cancel",
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert stopped.json()["state"] == "cancelled"
    after = (
        await api.get(f"/v1/plan-series/{successor['series']['id']}", headers=owner.headers)
    ).json()
    assert {item["state"] for item in after["occurrences"]} == {"cancelled"}


async def test_invalid_series_requests_are_rejected(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider)
    base = {
        "title": "Coffee",
        "base_currency": "USD",
        "timezone": "UTC",
        "start_date": "2026-11-02",
    }
    for rule in ("FREQ=HOURLY", "FREQ=WEEKLY;BYHOUR=9", "FREQ=DAILY;COUNT=2;UNTIL=20270101"):
        response = await api.post(
            "/v1/plan-series", json={**base, "recurrence_rule": rule}, headers=owner.headers
        )
        assert response.status_code == 422, rule
    no_time = await api.post(
        "/v1/plan-series",
        json={**base, "recurrence_rule": "FREQ=WEEKLY", "duration_minutes": 30},
        headers=owner.headers,
    )
    assert no_time.status_code == 422


async def test_duplicate_copies_only_the_allowlisted_manifest(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    friend = await sign_in(api, identity_provider, name="Friend")
    leaver = await sign_in(api, identity_provider, name="Leaver")
    source = (
        await api.post(
            "/v1/plans",
            json={
                "title": "Da Lat trip",
                "kind": "trip",
                "base_currency": "VND",
                "description": "Pack warm clothes",
                "location_label": "Hotel 12 Tran Phu",
                "participants": [{"placeholder_name": "Cousin"}],
            },
            headers=owner.headers,
        )
    ).json()
    invite = (
        await api.post(f"/v1/plans/{source['id']}/invites", json={}, headers=owner.headers)
    ).json()
    for user in (friend, leaver):
        await api.post("/v1/invites/redeem", json={"token": invite["token"]}, headers=user.headers)
    await api.post("/v1/invites/redeem", json={"token": invite["token"], "display_name": "Guest"})
    await api.post(f"/v1/plans/{source['id']}/leave", headers=leaver.headers)
    await api.put(
        f"/v1/plans/{source['id']}/rsvp", json={"status": "going"}, headers=friend.headers
    )
    travel = f"/v1/plans/{source['id']}/travel"
    await api.put(travel, json={"destination_summary": "Da Lat"}, headers=owner.headers)
    await api.post(
        f"{travel}/segments",
        json={"segment_type": "bus", "timing_mode": "date", "start_date": "2026-12-20"},
        headers=owner.headers,
    )
    for version, state in ((1, "active"), (2, "completed")):
        await api.post(
            f"/v1/plans/{source['id']}/state",
            json={"state": state},
            headers={**owner.headers, "If-Match": f'"{version}"'},
        )

    copied = await api.post(
        f"/v1/plans/{source['id']}/duplicate",
        json={"title": "Da Lat again"},
        headers=owner.headers,
    )
    assert copied.status_code == 201, copied.text
    plan = copied.json()
    assert (plan["title"], plan["state"], plan["kind"]) == ("Da Lat again", "planning", "trip")
    assert plan["description"] == "Pack warm clothes"
    assert plan["location_label"] is None
    roster = (await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)).json()
    assert sorted((p["display_name"], p["role"], p["rsvp_status"]) for p in roster) == [
        ("Friend", "member", "invited"),
        ("Owner", "owner", "invited"),
    ]
    new_travel = (await api.get(f"/v1/plans/{plan['id']}/travel", headers=owner.headers)).json()
    assert new_travel["destination_summary"] == "Da Lat"
    assert new_travel["segments"] == []
    assert (await api.get(f"/v1/plans/{plan['id']}/invites", headers=owner.headers)).json() == []
    assert (
        admin.scalar(
            "SELECT metadata->>'manifest' FROM sync_audit.audit_events "
            "WHERE action = 'plan.duplicated'"
        )
        == "plan-copy-v1"
    )

    none_selected = await api.post(
        f"/v1/plans/{source['id']}/duplicate",
        json={"participant_ids": []},
        headers=owner.headers,
    )
    copied_roster = await api.get(
        f"/v1/plans/{none_selected.json()['id']}/participants", headers=owner.headers
    )
    assert [p["role"] for p in copied_roster.json()] == ["owner"]
    friend_view = await api.post(
        f"/v1/plans/{source['id']}/duplicate", json={}, headers=friend.headers
    )
    assert friend_view.status_code == 403
