"""Memories: everyone shares, organisers pick highlights, the recap shows them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules import media as media_module
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.media import photo_with_location, upload
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

pytestmark = pytest.mark.integration


@pytest.fixture
async def worker(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    database = Database.for_worker(live_settings)
    runtime = Runtime(
        settings=live_settings,
        database=database,
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    monkeypatch.setattr(tasks, "get_worker_runtime", lambda: runtime)
    yield runtime
    await database.close()


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan", "Eve"))


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def memory(
    api: httpx.AsyncClient, user: Any, trip: FinancePlan, **details: Any
) -> dict[str, Any]:
    sent = await upload(api, user, trip, photo_with_location(), kind="memory", memory=details)
    assert await tasks.process_media_upload.func(sent["id"]) == "ready"
    recorded: dict[str, Any] = sent
    return recorded


async def test_everyone_shares_memories_and_organisers_pick_highlights(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    worker: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, bea, dan, eve = trip.owner, *(trip.members[n] for n in ("Bea", "Dan", "Eve"))
    await ok(
        await api.patch(
            trip.path(f"/participants/{trip.people['Eve']}"),
            json={"role": "viewer"},
            headers=if_match(1, owner),
        )
    )
    place = await ok(
        await api.post(trip.path("/places"), json={"name": "Fushimi Inari"}, headers=owner.headers),
        201,
    )
    # A viewer shares a photo too, with a caption, a day, a local time, and a place.
    sunrise = await memory(
        api,
        eve,
        trip,
        caption="Top of the mountain, finally",
        day="2027-03-21",
        taken_time="06:42:00",
        place_id=place["id"],
    )
    later = await memory(api, bea, trip, caption="Matcha", day="2027-03-21", taken_time="14:15:00")
    earlier = await memory(api, dan, trip, day="2027-03-20")
    listed = {
        row["id"]: row for row in await ok(await api.get(trip.path("/media"), headers=dan.headers))
    }
    assert listed[sunrise["id"]]["state"] == "ready"
    assert (listed[sunrise["id"]]["caption"], listed[sunrise["id"]]["place_id"]) == (
        "Top of the mountain, finally",
        place["id"],
    )

    # Its uploader and organisers edit it; others do not; stale versions are refused.
    path = trip.path(f"/media/{sunrise['id']}/memory")
    version = listed[sunrise["id"]]["version"]
    edit = {"caption": "Summit", "day": "2027-03-21", "taken_time": "06:42:00"}
    assert (await api.put(path, json=edit, headers=if_match(version, dan))).status_code == 403
    assert (await api.put(path, json=edit, headers=if_match(version - 1, eve))).status_code == 412
    edited = await ok(await api.put(path, json=edit, headers=if_match(version, eve)))
    assert (edited["caption"], edited["place_id"]) == ("Summit", None)
    await ok(
        await api.put(
            path, json={**edit, "caption": "Summit at dawn"}, headers=if_match(version + 1, owner)
        )
    )

    # Highlights: an organiser's pick, ready memories only, at most the limit.
    async def pick(media_id: str, user: Any = owner, on: bool = True) -> httpx.Response:
        return await api.put(
            trip.path(f"/media/{media_id}/highlight"), json={"in_recap": on}, headers=user.headers
        )

    assert (await pick(later["id"], bea)).status_code == 403
    for chosen in (later, sunrise, earlier):
        assert (await ok(await pick(chosen["id"])))["in_recap"] is True
    monkeypatch.setattr(media_module, "MAX_HIGHLIGHTS", 3)
    extra = await memory(api, bea, trip)
    limited = await pick(extra["id"])
    assert limited.status_code == 409 and limited.json()["code"] == "HIGHLIGHT_LIMIT_REACHED"
    awaiting = await ok(
        await api.post(
            trip.path("/media"),
            json={"kind": "memory", "content_type": "image/jpeg", "size_bytes": 10},
            headers=bea.headers,
        ),
        201,
    )
    assert (await pick(awaiting["id"])).status_code == 409

    # The recap shows the picks by day and time, with the cover on the card.
    cover = await upload(api, owner, trip, photo_with_location((10, 10, 10)), kind="cover")
    await tasks.process_media_upload.func(cover["id"])
    plan = await ok(await api.get(trip.path(), headers=owner.headers))
    await ok(
        await api.patch(
            trip.path(),
            json={"cover_media_id": cover["id"]},
            headers=if_match(plan["version"], owner),
        )
    )
    recap = await ok(await api.get(trip.path("/recap"), headers=eve.headers))
    assert recap["highlights"] == [earlier["id"], sunrise["id"], later["id"]]
    assert (recap["memories"], recap["cover_media_id"]) == (4, cover["id"])
    assert recap["share"]["cover_media_id"] == cover["id"]
    await ok(await pick(later["id"], on=False))
    assert (await ok(await api.get(trip.path("/recap"), headers=eve.headers)))["highlights"] == [
        earlier["id"],
        sunrise["id"],
    ]


async def test_memory_and_receipt_rules(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner, bea, dan, eve = trip.owner, *(trip.members[n] for n in ("Bea", "Dan", "Eve"))
    other = await finance_plan(api, identity_provider, admin, members=())
    elsewhere = await ok(
        await api.post(
            other.path("/places"), json={"name": "Elsewhere"}, headers=other.owner.headers
        ),
        201,
    )
    refused = [
        {
            "kind": "memory",
            "content_type": "image/jpeg",
            "size_bytes": 10,
            "memory": {"place_id": elsewhere["id"]},
        },
        {
            "kind": "memory",
            "content_type": "image/jpeg",
            "size_bytes": 10,
            "memory": {"taken_time": "09:00:00"},
        },
        {"kind": "memory", "content_type": "application/pdf", "size_bytes": 10},
    ]
    for body in refused:
        response = await api.post(trip.path("/media"), json=body, headers=bea.headers)
        assert response.status_code == 422, (body, response.text)
    hangout = await ok(
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Karaoke", "base_currency": "VND"},
            headers=owner.headers,
        ),
        201,
    )
    assert (
        await api.post(
            f"/v1/plans/{hangout['id']}/media",
            json={"kind": "memory", "content_type": "image/jpeg", "size_bytes": 10},
            headers=owner.headers,
        )
    ).status_code == 409

    # Receipts: never on a voided expense; deleted also by the expense's creator and by
    # whoever manages expenses.
    ann, bea_id = trip.people["Ann"], trip.people["Bea"]
    expense = await add_expense(api, bea, trip, equal_expense(900, bea_id, [ann, bea_id]))
    voided = await add_expense(api, bea, trip, equal_expense(100, bea_id, [bea_id]))
    gone = await api.post(trip.path(f"/expenses/{voided['id']}/void"), headers=if_match(1, bea))
    assert gone.status_code == 200, gone.text
    on_void = await api.post(
        trip.path("/media"),
        json={
            "kind": "receipt",
            "content_type": "image/png",
            "size_bytes": 10,
            "expense_id": voided["id"],
        },
        headers=dan.headers,
    )
    assert on_void.status_code == 409

    async def receipt() -> str:
        sent = await upload(api, dan, trip, photo_with_location(), expense_id=expense["id"])
        return str(sent["id"])

    first, second = await receipt(), await receipt()
    assert (await api.delete(trip.path(f"/media/{first}"), headers=eve.headers)).status_code == 403
    assert (await api.delete(trip.path(f"/media/{first}"), headers=bea.headers)).status_code == 204
    await ok(
        await api.patch(
            trip.path(f"/participants/{trip.people['Eve']}"),
            json={"capabilities": ["expenses.manage"]},
            headers=if_match(1, owner),
        )
    )
    assert (await api.delete(trip.path(f"/media/{second}"), headers=eve.headers)).status_code == 204


async def test_account_deletion_takes_memories_and_leaves_receipts(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    ann, bea_id = trip.people["Ann"], trip.people["Bea"]
    expense = await add_expense(api, owner, trip, equal_expense(400, ann, [ann, bea_id]))
    photo = await memory(api, bea, trip, caption="Me at the onsen")
    await ok(
        await api.put(
            trip.path(f"/media/{photo['id']}/highlight"),
            json={"in_recap": True},
            headers=owner.headers,
        )
    )
    proof = await upload(api, bea, trip, photo_with_location(), expense_id=expense["id"])
    _, cursor, _ = await pull_all(api, owner, f"plan:{trip.plan_id}")

    gone = await api.delete("/v1/me", headers=bea.headers)
    assert gone.status_code == 204, gone.text
    left = {
        row["id"]: row
        for row in await ok(await api.get(trip.path("/media"), headers=owner.headers))
    }
    assert photo["id"] not in left and proof["id"] in left
    items, _, _ = await pull_all(api, owner, f"plan:{trip.plan_id}", cursor)
    assert (photo["id"], "delete") in {
        (i["entity_id"], i["operation"]) for i in items if i["entity_type"] == "media"
    }
    assert (
        admin.scalar(
            "SELECT count(*) FROM media_memories.object_deletions WHERE object_key = %s",
            f"media/{photo['id']}",
        )
        == 1
    )
    assert (await ok(await api.get(trip.path("/recap"), headers=owner.headers)))["highlights"] == []


async def test_the_database_keeps_memories_honest(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, live_settings: Settings
) -> None:
    bea, dan = trip.members["Bea"], trip.members["Dan"]
    photo = await memory(api, bea, trip, caption="Mine")
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    attempts = [
        # A member picking a highlight, even of their own photo.
        (
            bea.user_id,
            "UPDATE media_memories.media SET in_recap = true, version = version + 1 WHERE id = %s",
        ),
        # A file that starts out picked for the recap.
        (
            bea.user_id,
            "INSERT INTO media_memories.media (id, plan_id, kind, state, declared_type, "
            "declared_size, uploaded_by_user_id, in_recap, version, created_at, updated_at) "
            "VALUES (gen_random_uuid(), %s, 'memory', 'awaiting_upload', 'image/jpeg', 10, %s, "
            "true, 1, now(), now())",
        ),
        # Someone else rewriting the caption.
        (
            dan.user_id,
            "UPDATE media_memories.media SET caption = 'Theirs', version = version + 1 "
            "WHERE id = %s",
        ),
    ]
    with psycopg.connect(dsn) as connection:
        for actor, statement in attempts:
            params = (
                (trip.plan_id, bea.user_id) if statement.startswith("INSERT") else (photo["id"],)
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor,))
                connection.execute(statement, params)
        # An organiser's pick goes through.
        with connection.transaction():
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (trip.owner.user_id,))
            picked = connection.execute(
                "UPDATE media_memories.media SET in_recap = true, version = version + 1 "
                "WHERE id = %s",
                (photo["id"],),
            )
            assert picked.rowcount == 1


async def test_memories_outlive_the_trip_until_it_is_archived_and_follow_their_people(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    photo = await memory(api, bea, trip, caption="Day one")
    admin.execute("UPDATE plans.plans SET state = 'completed' WHERE id = %s", trip.plan_id)
    after = await memory(api, bea, trip, caption="Back home")
    await ok(
        await api.put(
            trip.path(f"/media/{after['id']}/highlight"),
            json={"in_recap": True},
            headers=owner.headers,
        )
    )
    listed = {r["id"]: r for r in await ok(await api.get(trip.path("/media"), headers=bea.headers))}
    await ok(
        await api.put(
            trip.path(f"/media/{photo['id']}/memory"),
            json={"caption": "Day one, again"},
            headers=if_match(listed[photo["id"]]["version"], bea),
        )
    )
    admin.execute("UPDATE plans.plans SET state = 'archived' WHERE id = %s", trip.plan_id)
    archived = await api.post(
        trip.path("/media"),
        json={"kind": "memory", "content_type": "image/jpeg", "size_bytes": 10},
        headers=bea.headers,
    )
    assert archived.status_code == 403
    assert (
        await api.put(
            trip.path(f"/media/{after['id']}/highlight"),
            json={"in_recap": False},
            headers=owner.headers,
        )
    ).status_code == 403
    assert (
        await api.delete(trip.path(f"/media/{photo['id']}"), headers=bea.headers)
    ).status_code == 403


async def test_a_guest_keeps_their_memories_and_covers_outlive_their_uploader(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner, dan = trip.owner, trip.members["Dan"]
    invite = await ok(await api.post(trip.path("/invites"), json={}, headers=owner.headers), 201)
    joined = await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": invite["token"], "display_name": "Gia", "device": {"platform": "web"}},
        )
    )
    guest = signed_in_from(joined["session"])
    guest_photo = await memory(api, guest, trip, caption="As a guest")
    await sign_in(api, identity_provider, subject="gia-sub", name="Gia")
    account = signed_in_from(
        await ok(
            await api.post(
                "/v1/auth/google",
                json={"id_token": identity_provider.id_token(subject="gia-sub")},
                headers=guest.headers,
            )
        )
    )
    listed = {
        r["id"]: r for r in await ok(await api.get(trip.path("/media"), headers=owner.headers))
    }
    await ok(
        await api.put(
            trip.path(f"/media/{guest_photo['id']}/memory"),
            json={"caption": "Still mine"},
            headers=if_match(listed[guest_photo["id"]]["version"], account),
        )
    )
    assert (
        await api.delete(trip.path(f"/media/{guest_photo['id']}"), headers=account.headers)
    ).status_code == 204

    # Dan, an admin, sets the cover, then leaves Beluno: the trip keeps its cover.
    await ok(
        await api.patch(
            trip.path(f"/participants/{trip.people['Dan']}"),
            json={"role": "admin"},
            headers=if_match(1, owner),
        )
    )
    cover = await upload(api, dan, trip, photo_with_location((5, 5, 5)), kind="cover")
    await tasks.process_media_upload.func(cover["id"])
    plan = await ok(await api.get(trip.path(), headers=owner.headers))
    await ok(
        await api.patch(
            trip.path(),
            json={"cover_media_id": cover["id"]},
            headers=if_match(plan["version"], dan),
        )
    )
    gone = await api.delete("/v1/me", headers=dan.headers)
    assert gone.status_code == 204, gone.text
    assert (await ok(await api.get(trip.path(), headers=owner.headers)))["cover_media_id"] == cover[
        "id"
    ]
