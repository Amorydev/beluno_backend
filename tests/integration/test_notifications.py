"""Notifications reach the people activity involves, respecting settings and quiet hours."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.notifications import dispatch
from beluno.push import DisabledPushSender, PushResult
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


async def device(
    api: httpx.AsyncClient, user: SignedIn, token: str, settings: dict[str, Any] | None = None
) -> None:
    """Register the device, with quiet hours off unless the test says otherwise."""

    await ok(
        await api.put(
            "/v1/me/push-token", json={"token": token, "platform": "ios"}, headers=user.headers
        ),
        204,
    )
    await ok(
        await api.put(
            "/v1/me/notification-settings", json=settings or LOUD, headers=if_match(0, user)
        )
    )


async def run(worker: Runtime, sender: Any, at: datetime | None = None) -> int:
    moment = at or datetime.now(UTC)
    return await dispatch(replace(worker, clock=lambda: moment), sender)


def token(name: str) -> str:
    return f"fcm-token-of-{name}-0123456789"


async def test_money_reaches_the_people_it_involves_without_amounts(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann, bea_id = trip.people["Ann"], trip.people["Bea"]
    for user, name in ((owner, "ann"), (bea, "bea"), (dan, "dan")):
        await device(api, user, token(name))
    await add_expense(api, owner, trip, equal_expense(98_761, ann, [ann, bea_id]))

    sender = RecordingPushSender()
    assert await run(worker, sender) == 1
    [message] = sender.sent
    assert (message.token, message.kind, message.category) == (
        token("bea"),
        "expense_added",
        "money",
    )
    assert message.loc_args == ["Ann", "Trip"]
    assert message.data["plan_id"] == trip.plan_id
    shown = str([*message.loc_args, *message.data.values()])
    assert "98761" not in shown and "987.61" not in shown
    # Running again sends nothing twice.
    assert await run(worker, sender) == 0
    assert admin.scalar("SELECT count(*) FROM engagement.notifications") == 1
    assert admin.scalar("SELECT state FROM engagement.notifications") == "sent"


async def test_settings_quiet_hours_and_dead_tokens(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann, bea_id, dan_id = trip.people["Ann"], trip.people["Bea"], trip.people["Dan"]

    defaults = await api.get("/v1/me/notification-settings", headers=bea.headers)
    assert defaults.headers["etag"] == '"0"'
    assert defaults.json() == {**LOUD, "quiet_hours": True, "version": 0}
    assert (
        await api.put("/v1/me/notification-settings", json=LOUD, headers=bea.headers)
    ).status_code == 428
    # Bea turns money off; Dan is quiet around the clock.
    await device(api, bea, token("bea"), {**LOUD, "money": False})
    assert (
        await api.put("/v1/me/notification-settings", json=LOUD, headers=if_match(0, bea))
    ).status_code == 412
    await device(
        api,
        dan,
        token("dan"),
        {**LOUD, "quiet_hours": True, "quiet_start": "00:00:00", "quiet_end": "23:59:59"},
    )
    await add_expense(api, owner, trip, equal_expense(900, ann, [ann, bea_id, dan_id]))
    sender = RecordingPushSender()
    assert await run(worker, sender) == 0
    states = dict(
        admin.fetch("SELECT user_id::text, state FROM engagement.notifications ORDER BY user_id")
    )
    assert states == {bea.user_id: "skipped", dan.user_id: "pending"}
    assert admin.scalar(
        "SELECT deliver_after > now() FROM engagement.notifications WHERE user_id = %s",
        dan.user_id,
    )

    # A token FCM no longer knows is dropped; a busy FCM is retried later.
    await ok(await api.put("/v1/me/notification-settings", json=LOUD, headers=if_match(1, dan)))
    admin.execute("UPDATE engagement.notifications SET deliver_after = now()")
    sender = RecordingPushSender(answers={token("dan"): PushResult.INVALID_TOKEN})
    await run(worker, sender)
    assert len(sender.to(token("dan"))) == 1
    assert (
        admin.scalar("SELECT count(*) FROM engagement.push_tokens WHERE user_id = %s", dan.user_id)
        == 0
    )
    await ok(
        await api.put(
            "/v1/me/push-token",
            json={"token": token("dan-new"), "platform": "android"},
            headers=dan.headers,
        ),
        204,
    )
    await add_expense(api, owner, trip, equal_expense(300, ann, [ann, dan_id]))
    busy = RecordingPushSender(answers={token("dan-new"): PushResult.RETRY})
    await run(worker, busy)
    assert admin.fetch(
        "SELECT state, attempts, deliver_after > now() FROM engagement.notifications "
        "WHERE user_id = %s AND state <> 'failed' ORDER BY created_at DESC LIMIT 1",
        dan.user_id,
    ) == [("pending", 1, True)]


async def test_reminders_for_tasks_due_and_polls_closing(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    await device(api, bea, token("bea"))
    await device(api, dan, token("dan"))
    await ok(
        await api.post(
            trip.path("/tasks"),
            json={
                "title": "Print vouchers",
                "assignee_participant_id": trip.people["Bea"],
                "due_date": datetime.now(UTC).date().isoformat(),
            },
            headers=owner.headers,
        ),
        201,
    )
    poll = await ok(
        await api.post(
            trip.path("/polls"),
            json={
                "question": "Dinner?",
                "options": [{"label": "Ramen"}, {"label": "Sushi"}],
                "deadline_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
            headers=owner.headers,
        ),
        201,
    )
    await ok(
        await api.put(
            trip.path(f"/polls/{poll['id']}/vote"),
            json={"option_id": poll["options"][0]["id"]},
            headers=bea.headers,
        )
    )
    admin.execute(
        "UPDATE decisions.polls SET deadline_at = now() + interval '1 hour' WHERE id = %s",
        poll["id"],
    )
    sender = RecordingPushSender()
    await run(worker, sender)
    assert sorted((m.token, m.kind) for m in sender.sent) == [
        (token("bea"), "task_due"),
        (token("dan"), "poll_closing"),
    ]
    assert all(m.category == "reminders" and m.loc_args == ["Trip"] for m in sender.sent)
    await run(worker, sender)
    assert len(sender.sent) == 2


async def test_tokens_follow_sessions_and_accounts(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    admin: AdminDatabase,
) -> None:
    bea = trip.members["Bea"]
    await device(api, bea, token("shared"))
    # The same phone signs in to another account: the token moves there.
    other = await sign_in(api, identity_provider, name="Other")
    await device(api, other, token("shared"))
    assert admin.fetch("SELECT user_id::text FROM engagement.push_tokens") == [(other.user_id,)]
    await ok(await api.delete("/v1/me/push-token", headers=other.headers), 204)
    assert admin.scalar("SELECT count(*) FROM engagement.push_tokens") == 0

    await ok(
        await api.put(
            "/v1/me/push-token",
            json={"token": token("bea"), "platform": "ios"},
            headers=bea.headers,
        ),
        204,
    )
    await ok(await api.post("/v1/auth/logout", headers=bea.headers), 204)
    assert admin.scalar("SELECT count(*) FROM engagement.push_tokens") == 0
    refused = await api.put(
        "/v1/me/push-token", json={"token": "short", "platform": "ios"}, headers=other.headers
    )
    assert refused.status_code == 422

    dan = trip.members["Dan"]
    await device(api, dan, token("dan"), {**LOUD, "news": True})
    gone = await api.delete("/v1/me", headers=dan.headers)
    assert gone.status_code == 204, gone.text
    assert (
        admin.scalar(
            "SELECT count(*) FROM engagement.notification_settings WHERE user_id = %s", dan.user_id
        )
        == 0
    )
    assert admin.scalar("SELECT count(*) FROM engagement.push_tokens") == 0


async def test_payments_merged_people_devices_and_live_sessions(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    ann, cam, dan_id = trip.people["Ann"], trip.people["Cam"], trip.people["Dan"]
    await device(api, bea, token("bea"))
    # An expense shared with placeholder Cam; then Cam turns out to be Bea.
    shared = await add_expense(api, owner, trip, equal_expense(500, ann, [ann, cam]))
    await run(worker, RecordingPushSender())
    claim = await ok(
        await api.post(
            trip.path(f"/participants/{cam}/claim-invites"), json={}, headers=owner.headers
        ),
        201,
    )
    await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": claim["token"], "merge_existing": True},
            headers=bea.headers,
        )
    )
    voided = await api.post(trip.path(f"/expenses/{shared['id']}/void"), headers=if_match(1, owner))
    assert voided.status_code == 200, voided.text
    # A payment from Dan to Ann reaches Ann; Dan, who recorded it, hears nothing.
    await device(api, owner, token("ann"))
    await device(api, dan, token("dan"))
    paid = await api.post(
        trip.path("/settlements"),
        json={
            "from_participant_id": dan_id,
            "to_participant_id": ann,
            "currency": "USD",
            "amount_minor": 100,
            "occurred_on": "2030-06-01",
        },
        headers=dan.headers,
    )
    assert paid.status_code == 201, paid.text
    sender = RecordingPushSender()
    await run(worker, sender)
    assert sorted((m.token, m.kind) for m in sender.sent) == [
        (token("ann"), "payment_recorded"),
        (token("bea"), "expense_voided"),
    ]
    payment = next(m for m in sender.sent if m.kind == "payment_recorded")
    assert payment.loc_args == ["Dan", "Trip"]

    # A session past its lifetime no longer gets anything; FCM not configured skips.
    admin.execute(
        "UPDATE iam.sessions SET idle_expires_at = now() - interval '1 minute' WHERE id = %s",
        dan.session_id,
    )
    await add_expense(api, owner, trip, equal_expense(700, ann, [ann, dan_id]))
    sender = RecordingPushSender()
    await run(worker, sender)
    assert sender.sent == []
    assert (
        admin.scalar(
            "SELECT state FROM engagement.notifications WHERE user_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            dan.user_id,
        )
        == "skipped"
    )


async def test_stuck_and_unconfigured_deliveries_end(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    ann, bea_id = trip.people["Ann"], trip.people["Bea"]
    await device(api, bea, token("bea"))
    await add_expense(api, owner, trip, equal_expense(500, ann, [ann, bea_id]))
    await run(worker, DisabledPushSender())
    assert admin.scalar("SELECT state FROM engagement.notifications") == "skipped"

    # A delivery a crashed worker left half-done is retried, then given up.
    await add_expense(api, owner, trip, equal_expense(600, ann, [ann, bea_id]))
    await run(worker, RecordingPushSender(answers={token("bea"): PushResult.RETRY}))
    admin.execute(
        "UPDATE engagement.notifications SET state = 'sending', attempts = 5, "
        "deliver_after = now() - interval '1 hour' WHERE state = 'pending'"
    )
    await run(worker, RecordingPushSender())
    assert sorted(r[0] for r in admin.fetch("SELECT state FROM engagement.notifications")) == [
        "failed",
        "skipped",
    ]


async def test_reminders_respect_plan_state_and_expire(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    # Quiet all day: the reminder waits, and comes too late.
    await device(
        api,
        bea,
        token("bea"),
        {**LOUD, "quiet_hours": True, "quiet_start": "00:00:00", "quiet_end": "23:59:59"},
    )
    await ok(
        await api.post(
            trip.path("/tasks"),
            json={
                "title": "Pack",
                "assignee_participant_id": trip.people["Bea"],
                "due_date": datetime.now(UTC).date().isoformat(),
            },
            headers=owner.headers,
        ),
        201,
    )
    admin.execute("UPDATE plans.plans SET state = 'archived' WHERE id = %s", trip.plan_id)
    sender = RecordingPushSender()
    await run(worker, sender)
    assert sender.sent == []
    assert admin.scalar("SELECT count(*) FROM engagement.notifications") == 0
    admin.execute("UPDATE plans.plans SET state = 'active' WHERE id = %s", trip.plan_id)
    await run(worker, sender)
    assert admin.scalar("SELECT state FROM engagement.notifications") == "pending"
    admin.execute("UPDATE engagement.notifications SET deliver_after = now()")
    await run(worker, sender, at=datetime.now(UTC) + timedelta(days=3))
    assert sender.sent == []
    assert admin.scalar("SELECT state FROM engagement.notifications") == "skipped"


async def test_people_never_see_each_others_tokens_or_settings(
    api: httpx.AsyncClient, trip: FinancePlan, live_settings: Settings
) -> None:
    bea, dan = trip.members["Bea"], trip.members["Dan"]
    await device(api, bea, token("bea"), {**LOUD, "news": True})
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (dan.user_id,))
        for table in ("engagement.push_tokens", "engagement.notification_settings"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        deleted = connection.execute("DELETE FROM engagement.push_tokens").rowcount
        assert deleted == 0
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (dan.user_id,))
        connection.execute("SELECT count(*) FROM engagement.notifications")
