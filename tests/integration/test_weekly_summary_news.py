"""The Sunday planning summary and news from the team."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from sqlalchemy.exc import DBAPIError

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules import notifications
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.notifications import News, dispatch, send_news
from beluno.testkit.api_client import SignedIn
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

QUIET_OFF = {
    "money": False,
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
            json={"token": f"fcm-token-of-{name}-0123456789", "platform": "android"},
            headers=user.headers,
        ),
        204,
    )
    await ok(
        await api.put(
            "/v1/me/notification-settings",
            json={**QUIET_OFF, **settings},
            headers=if_match(0, user),
        )
    )


def last_sunday_evening() -> datetime:
    """The latest Sunday 20:00 UTC that has passed."""

    now = datetime.now(UTC)
    sunday = now.date() - timedelta(days=(now.weekday() + 1) % 7)
    moment = datetime(sunday.year, sunday.month, sunday.day, 20, tzinfo=UTC)
    return moment if moment <= now else moment - timedelta(days=7)


async def run(worker: Runtime, sender: RecordingPushSender, moment: datetime) -> None:
    await dispatch(replace(worker, clock=lambda: moment), sender)


def received(sender: RecordingPushSender, name: str) -> list[tuple[str, list[str]]]:
    return [(m.kind, m.loc_args) for m in sender.to(f"fcm-token-of-{name}-0123456789")]


async def test_sunday_evening_sums_up_each_trip_being_organised(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    await device(api, bea, "bea")
    await device(api, dan, "dan", summaries=False)
    sunday = last_sunday_evening()
    starts = sunday.date() + timedelta(days=12)
    plan = await ok(await api.get(trip.path(), headers=owner.headers))
    await ok(
        await api.patch(
            trip.path(),
            json={"timing": {"mode": "date", "start_date": starts.isoformat()}},
            headers=if_match(plan["version"], owner),
        )
    )
    for title in ("Book the van", "Buy the tickets"):
        await ok(
            await api.post(trip.path("/tasks"), json={"title": title}, headers=owner.headers),
            201,
        )

    sender = RecordingPushSender()
    await run(worker, sender, sunday - timedelta(days=1))  # Saturday: nothing
    assert received(sender, "bea") == []
    await run(worker, sender, sunday - timedelta(hours=2))  # Sunday 18:00: not yet
    assert received(sender, "bea") == []
    await run(worker, sender, sunday)
    assert received(sender, "bea") == [("weekly_summary", ["Trip", "2", "0", "12"])]
    assert received(sender, "dan") == []  # summaries off
    await run(worker, sender, sunday + timedelta(minutes=30))
    assert len(received(sender, "bea")) == 1  # once per Sunday

    # A trip under way gets the daily summary instead.
    admin.execute("UPDATE plans.plans SET state = 'active' WHERE id = %s", trip.plan_id)
    later = RecordingPushSender()
    await run(worker, later, sunday + timedelta(days=7))
    assert [kind for kind, _ in received(later, "bea")] == []


async def test_a_trip_with_nothing_to_say_sends_nothing(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime
) -> None:
    await device(api, trip.members["Bea"], "bea")
    sender = RecordingPushSender()
    await run(worker, sender, last_sunday_evening())
    assert received(sender, "bea") == []


async def test_news_reaches_those_who_want_it_in_their_language(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    await device(api, owner, "ann", news=True)
    # Signed out of every device: nothing is queued for later.
    await ok(await api.delete("/v1/me/push-token", headers=owner.headers), 204)
    await device(api, bea, "bea", news=True)
    await device(api, dan, "dan", news=True)
    profile = await ok(await api.get("/v1/me", headers=bea.headers))
    await ok(
        await api.patch(
            "/v1/me", json={"locale": "vi-VN"}, headers=if_match(profile["version"], bea)
        )
    )
    news = News(
        key="trip-pass-launch",
        title_vi="Trip Pass đã có",
        body_vi="Mở khoá một chuyến đi cho cả nhóm.",
        title_en="Trip Pass is here",
        body_en="Unlock one trip for everyone on it.",
    )
    assert await send_news(worker, news, operator="tester") == 2
    assert await send_news(worker, news, operator="tester") == 0  # a key is sent once
    sender = RecordingPushSender()
    await run(worker, sender, datetime.now(UTC))
    by_token = {
        message.token: (message.kind, message.title, message.body) for message in sender.sent
    }
    assert by_token == {
        "fcm-token-of-bea-0123456789": (
            "news",
            "Trip Pass đã có",
            "Mở khoá một chuyến đi cho cả nhóm.",
        ),
        "fcm-token-of-dan-0123456789": (
            "news",
            "Trip Pass is here",
            "Unlock one trip for everyone on it.",
        ),
    }
    for bad in (
        replace(news, key="Not A Slug"),
        replace(news, key="too-long", title_en="x" * 81),
    ):
        with pytest.raises(DBAPIError) as refused:
            await send_news(worker, bad, operator="tester")
        assert isinstance(refused.value.orig, psycopg.errors.InvalidParameterValue)
    with pytest.raises(ValueError):
        await send_news(worker, replace(news, key="blank", body_vi="   "), operator="tester")


async def test_each_person_gets_it_on_their_own_sunday_evening(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    for user, zone in ((bea, "Asia/Ho_Chi_Minh"), (dan, "America/Los_Angeles")):
        profile = await ok(await api.get("/v1/me", headers=user.headers))
        await ok(
            await api.patch(
                "/v1/me", json={"timezone": zone}, headers=if_match(profile["version"], user)
            )
        )
    await device(api, bea, "bea")
    await device(api, dan, "dan")
    sunday = last_sunday_evening().date() - timedelta(days=7)
    vietnam, california = ZoneInfo("Asia/Ho_Chi_Minh"), ZoneInfo("America/Los_Angeles")
    starts = datetime.combine(sunday + timedelta(days=10), time(9), vietnam)
    plan = await ok(await api.get(trip.path(), headers=owner.headers))
    await ok(
        await api.patch(
            trip.path(),
            json={
                "timing": {
                    "mode": "datetime",
                    "starts_at": starts.isoformat(),
                    "timezone": "Asia/Ho_Chi_Minh",
                }
            },
            headers=if_match(plan["version"], owner),
        )
    )
    await ok(
        await api.post(
            trip.path("/polls"), json={"kind": "yes_no", "question": "Go?"}, headers=owner.headers
        ),
        201,
    )
    for zone, name in ((vietnam, "bea"), (california, "dan")):
        evening = datetime.combine(sunday, time(19), zone)
        sender = RecordingPushSender()
        await run(worker, sender, evening - timedelta(minutes=1))
        assert received(sender, name) == [], name
        await run(worker, sender, evening)
        assert received(sender, name) == [("weekly_summary", ["Trip", "0", "1", "10"])], name


async def test_a_news_burst_never_holds_up_money(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    await device(api, bea, "bea", money=True, news=True)
    admin.execute(
        "INSERT INTO engagement.notifications (id, user_id, category, kind, args, dedupe_key,"
        " state, attempts, deliver_after, created_at)"
        " SELECT gen_random_uuid(), %s, 'news', 'news',"
        " jsonb_build_object('title', 'T', 'body', 'B'), 'news:burst:' || n, 'pending', 0,"
        " now() - interval '1 hour', now() FROM generate_series(1, 150) AS n",
        bea.user_id,
    )
    await add_expense(
        api, owner, trip, equal_expense(400, trip.people["Ann"], [trip.people["Bea"]])
    )
    monkeypatch.setattr(notifications, "MAX_BATCHES", 1)
    sender = RecordingPushSender()
    await run(worker, sender, datetime.now(UTC))
    kinds = [kind for kind, _ in received(sender, "bea")]
    assert "expense_added" in kinds and len(kinds) == notifications.BATCH
    # With several batches a run, the rest goes out too.
    monkeypatch.setattr(notifications, "MAX_BATCHES", 10)
    await run(worker, sender, datetime.now(UTC))
    assert len(received(sender, "bea")) == 151
