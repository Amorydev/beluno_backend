"""Nudges, forgiven debts, the 21:00 summary, and signing out other devices."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.notifications import dispatch
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.push import RecordingPushSender
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration

LOUD = {
    "money": True,
    "reminders": True,
    "summaries": True,
    "news": False,
    "quiet_hours": False,
    "quiet_start": "22:00:00",
    "quiet_end": "07:00:00",
}


@pytest.fixture
async def worker(live_settings: Settings) -> AsyncIterator[Runtime]:
    database = Database.for_worker(live_settings)
    yield Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    await database.close()


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def device(api: httpx.AsyncClient, user: SignedIn, name: str, **settings: Any) -> None:
    await ok(
        await api.put(
            "/v1/me/push-token",
            json={"token": f"fcm-token-of-{name}-0123456789", "platform": "ios"},
            headers=user.headers,
        ),
        204,
    )
    await ok(
        await api.put(
            "/v1/me/notification-settings",
            json={**LOUD, **settings},
            headers=if_match(0, user),
        )
    )


async def run(worker: Runtime, sender: RecordingPushSender) -> None:
    moment = datetime.now(UTC)
    await dispatch(replace(worker, clock=lambda: moment), sender)


def sent(sender: RecordingPushSender, name: str) -> list[tuple[str, list[str]]]:
    return [(m.kind, m.loc_args) for m in sender.to(f"fcm-token-of-{name}-0123456789")]


async def test_nudges_reach_the_right_person_once_a_day(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann, dan_id = trip.people["Ann"], trip.people["Dan"]
    for user, name in ((owner, "ann"), (bea, "bea"), (dan, "dan")):
        await device(api, user, name)
    task = await ok(
        await api.post(
            trip.path("/tasks"),
            json={"title": "Book the van", "assignee_participant_id": trip.people["Bea"]},
            headers=owner.headers,
        ),
        201,
    )
    nudge = trip.path(f"/tasks/{task['id']}/nudge")
    assert (await api.post(nudge, headers=dan.headers)).status_code == 403
    assert (await ok(await api.post(nudge, headers=owner.headers)))["queued"] is True
    assert (await ok(await api.post(nudge, headers=owner.headers)))["queued"] is False

    # Dan owes Ann; only Ann (who is owed) can nudge him, and nobody nudges Ann.
    await add_expense(api, owner, trip, equal_expense(1_000, ann, [ann, dan_id]))
    ledger_nudge = trip.path("/ledger/nudges")
    assert (
        await ok(
            await api.post(ledger_nudge, json={"participant_id": dan_id}, headers=owner.headers)
        )
    )["queued"] is True
    for user, target in ((dan, ann), (bea, dan_id)):
        refused = await api.post(
            ledger_nudge, json={"participant_id": target}, headers=user.headers
        )
        assert refused.status_code == 409, refused.text

    sender = RecordingPushSender()
    await run(worker, sender)
    assert sent(sender, "bea") == [("task_nudge", ["Ann", "Trip"])]
    assert sorted(sent(sender, "dan")) == [
        ("expense_added", ["Ann", "Trip"]),
        ("payment_nudge", ["Ann", "Trip"]),
    ]
    assert sent(sender, "ann") == []

    # A done task is not nudged any more.
    await ok(
        await api.post(
            trip.path(f"/tasks/{task['id']}/status"), json={"status": "done"}, headers=bea.headers
        )
    )
    assert (await api.post(nudge, headers=owner.headers)).status_code == 409


async def task_for(api: httpx.AsyncClient, trip: FinancePlan, assignee: str, **extra: Any) -> str:
    created = await ok(
        await api.post(
            trip.path("/tasks"),
            json={"title": f"For {assignee}", "assignee_participant_id": assignee, **extra},
            headers=trip.owner.headers,
        ),
        201,
    )
    return str(created["id"])


async def test_nudges_only_go_to_someone_who_can_get_them(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    ann, cam = trip.people["Ann"], trip.people["Cam"]
    await device(api, bea, "bea")

    def nudge(task_id: str) -> str:
        return trip.path(f"/tasks/{task_id}/nudge")

    # A name-only placeholder and the nudger themself cannot get one.
    for assignee in (cam, ann):
        refused = await api.post(nudge(await task_for(api, trip, assignee)), headers=owner.headers)
        assert refused.status_code == 409, refused.text
    await add_expense(api, owner, trip, equal_expense(1_000, ann, [ann, cam]))
    refused = await api.post(
        trip.path("/ledger/nudges"), json={"participant_id": cam}, headers=owner.headers
    )
    assert refused.status_code == 409, refused.text

    # Once the placeholder is merged into Bea, her task's nudge reaches Bea.
    cams_task = await task_for(api, trip, cam)
    admin.execute(
        "UPDATE plans.plan_participants SET access_state = 'merged',"
        " merged_into_participant_id = %s WHERE id = %s",
        trip.people["Bea"],
        cam,
    )
    assert (await ok(await api.post(nudge(cams_task), headers=owner.headers)))["queued"] is True
    sender = RecordingPushSender()
    await run(worker, sender)
    assert ("task_nudge", ["Ann", "Trip"]) in sent(sender, "bea")


async def test_a_forgiven_debt_tells_the_debtor(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime
) -> None:
    owner, dan = trip.owner, trip.members["Dan"]
    ann, dan_id = trip.people["Ann"], trip.people["Dan"]
    await device(api, owner, "ann")
    await device(api, dan, "dan")
    await add_expense(api, owner, trip, equal_expense(1_000, ann, [ann, dan_id]))
    await run(worker, RecordingPushSender())
    await ok(
        await api.post(
            trip.path("/waivers"),
            json={
                "debtor_participant_id": dan_id,
                "creditor_participant_id": ann,
                "currency": "USD",
                "amount_minor": 500,
                "occurred_on": "2027-03-20",
            },
            headers=owner.headers,
        ),
        201,
    )
    sender = RecordingPushSender()
    await run(worker, sender)
    assert sent(sender, "dan") == [("waiver_given", ["Ann", "Trip"])]
    assert sent(sender, "ann") == []


def zone_at(hour: int) -> str:
    """A time zone where it is ``hour``-something right now (Etc/GMT signs are inverted)."""

    offset = hour - datetime.now(UTC).hour
    if offset > 14:
        offset -= 24
    if offset < -12:
        offset += 24
    return "UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"


async def set_zone(api: httpx.AsyncClient, user: SignedIn, zone: str) -> None:
    profile = await ok(await api.get("/v1/me", headers=user.headers))
    await ok(
        await api.patch(
            "/v1/me", json={"timezone": zone}, headers=if_match(profile["version"], user)
        )
    )


async def test_the_evening_summary_counts_what_others_did_today(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    worker: Runtime,
) -> None:
    trip = await finance_plan(api, identity_provider, admin, members=("Bea", "Dan", "Eve"))
    owner, bea, dan, eve = (trip.owner, *(trip.members[n] for n in ("Bea", "Dan", "Eve")))
    ann, bea_id = trip.people["Ann"], trip.people["Bea"]
    for user in (bea, dan):
        await set_zone(api, user, zone_at(22))
    await set_zone(api, eve, zone_at(10))  # her evening has not come yet
    await device(api, bea, "bea", money=False)
    await device(api, dan, "dan", summaries=False)
    await device(api, eve, "eve", money=False)
    admin.execute("UPDATE plans.plans SET state = 'active' WHERE id = %s", trip.plan_id)
    await add_expense(api, owner, trip, equal_expense(400, ann, [ann, bea_id]))
    await add_expense(api, bea, trip, equal_expense(200, bea_id, [ann, bea_id]))
    by_others = admin.scalar(
        "SELECT count(*) FROM activity.events WHERE plan_id = %s AND scope_type = 'plan'"
        " AND actor_user_id IS DISTINCT FROM %s",
        trip.plan_id,
        bea.user_id,
    )

    sender = RecordingPushSender()
    await run(worker, sender)
    assert sent(sender, "bea") == [("daily_summary", ["Trip", str(by_others)])]
    assert sent(sender, "eve") == []
    queued_for = {
        str(row[0])
        for row in admin.fetch(
            "SELECT user_id FROM engagement.notifications WHERE kind = 'daily_summary'"
            " AND plan_id = %s",
            trip.plan_id,
        )
    }
    # Not Dan (summaries off) nor Eve (morning); Ann's own evening depends on UTC's clock.
    assert queued_for - {owner.user_id} == {bea.user_id}
    await run(worker, sender)
    assert len(sent(sender, "bea")) == 1


async def test_a_time_zone_the_database_lacks_does_not_stop_notifications(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann, bea_id, dan_id = trip.people["Ann"], trip.people["Bea"], trip.people["Dan"]
    await device(api, bea, "bea")
    await device(api, dan, "dan")
    # Python knows more zone names than some PostgreSQL builds; Dan has one of those.
    admin.execute("UPDATE iam.users SET timezone = 'Nowhere/Atlantis' WHERE id = %s", dan.user_id)
    admin.execute("UPDATE plans.plans SET state = 'active' WHERE id = %s", trip.plan_id)
    await task_for(api, trip, dan_id, due_date=datetime.now(UTC).date().isoformat())
    nudged = await task_for(api, trip, dan_id)
    nudge = await ok(await api.post(trip.path(f"/tasks/{nudged}/nudge"), headers=owner.headers))
    assert nudge["queued"] is True
    await add_expense(api, owner, trip, equal_expense(300, ann, [ann, bea_id, dan_id]))

    sender = RecordingPushSender()
    await run(worker, sender)
    # (After 21:00 UTC a daily summary may join them; it is not what this checks.)
    assert [m for m in sent(sender, "bea") if m[0] != "daily_summary"] == [
        ("expense_added", ["Ann", "Trip"])
    ]
    assert sorted(kind for kind, _ in sent(sender, "dan") if kind != "daily_summary") == [
        "expense_added",
        "task_due",
        "task_nudge",
    ]


async def test_task_reminders_follow_the_assignees_own_day(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime
) -> None:
    bea, bea_id = trip.members["Bea"], trip.people["Bea"]
    now = datetime.now(UTC)
    # A zone where the date differs from UTC's, and a due date that is today or tomorrow
    # there but neither in UTC.
    if now.hour < 12:
        zone, due = "Etc/GMT+12", now.date() - timedelta(days=1)
    else:
        zone, due = "Etc/GMT-14", now.date() + timedelta(days=2)
    await set_zone(api, bea, zone)
    await device(api, bea, "bea")
    await task_for(api, trip, bea_id, due_date=due.isoformat())

    sender = RecordingPushSender()
    await run(worker, sender)
    assert [kind for kind, _ in sent(sender, "bea")] == ["task_due"]


async def test_signing_out_every_other_device(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    phone = await sign_in(api, identity_provider, subject="kim-sub", name="Kim")
    laptop = await sign_in(api, identity_provider, subject="kim-sub", name="Kim")
    tablet = await sign_in(api, identity_provider, subject="kim-sub", name="Kim")
    assert phone.user_id == laptop.user_id == tablet.user_id
    await ok(await api.post("/v1/me/sessions/sign-out-others", headers=phone.headers), 204)
    assert (await api.get("/v1/me", headers=phone.headers)).status_code == 200
    for other in (laptop, tablet):
        assert (await api.get("/v1/me", headers=other.headers)).status_code == 401
    listed = await ok(await api.get("/v1/me/sessions", headers=phone.headers))
    assert [row["id"] for row in listed] == [phone.session_id]


async def test_two_devices_signing_out_the_others_at_once_leave_one_signed_in(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    phone = await sign_in(api, identity_provider, subject="lee-sub", name="Lee")
    laptop = await sign_in(api, identity_provider, subject="lee-sub", name="Lee")
    answers = await asyncio.gather(
        *(
            api.post("/v1/me/sessions/sign-out-others", headers=device.headers)
            for device in (phone, laptop)
        )
    )
    assert sorted(answer.status_code for answer in answers) == [204, 401]
    still_in = [
        device
        for device in (phone, laptop)
        if (await api.get("/v1/me", headers=device.headers)).status_code == 200
    ]
    assert len(still_in) == 1
