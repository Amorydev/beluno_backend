"""Files go to storage through signed URLs, and are scanned and cleaned before use."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import boto3
import httpx
import psycopg
import pytest
from botocore.exceptions import ClientError
from PIL import Image

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.malware import ClamdScanner, ScannerUnavailable
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.testkit.api_client import SignedIn, sign_in
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
from beluno.testkit.media import EICAR
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

pytestmark = pytest.mark.integration

GPS_TAG = 0x8825


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
    return await finance_plan(api, identity_provider, admin, members=("Bea", "Dan"))


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def photo_with_location() -> bytes:
    """A JPEG carrying a camera model and GPS coordinates in its EXIF."""

    image = Image.new("RGB", (64, 48), (200, 80, 40))
    exif = Image.Exif()
    exif[0x0110] = "Pixel 9"  # camera model
    exif[GPS_TAG] = {1: "N", 2: (35.0, 0.0, 0.0), 3: "E", 4: (135.0, 0.0, 0.0)}
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif.tobytes())
    return output.getvalue()


async def upload(
    api: httpx.AsyncClient,
    user: SignedIn,
    trip: FinancePlan,
    data: bytes,
    *,
    kind: str = "receipt",
    content_type: str = "image/jpeg",
    expense_id: str | None = None,
) -> dict[str, Any]:
    """Record the file, PUT it to storage with the signed URL, and report it uploaded."""

    body = {"kind": kind, "content_type": content_type, "size_bytes": len(data)}
    if expense_id:
        body["expense_id"] = expense_id
    media = await ok(await api.post(trip.path("/media"), json=body, headers=user.headers), 201)
    signed = await ok(
        await api.post(trip.path(f"/media/{media['id']}/upload-url"), headers=user.headers)
    )
    async with httpx.AsyncClient() as storage:
        put = await storage.put(
            signed["url"],
            content=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
        )
    assert put.status_code == 200, put.text
    uploaded: dict[str, Any] = await ok(
        await api.post(trip.path(f"/media/{media['id']}/uploaded"), headers=user.headers)
    )
    return uploaded


def stored(settings: Settings, key: str) -> bool:
    client = boto3.client(
        "s3",
        endpoint_url=settings.storage_endpoint_url,
        region_name=settings.storage_region,
        aws_access_key_id=settings.storage_access_key_id.get_secret_value(),  # type: ignore[union-attr]
        aws_secret_access_key=settings.storage_secret_access_key.get_secret_value(),  # type: ignore[union-attr]
    )
    try:
        client.head_object(Bucket=settings.storage_bucket, Key=key)
    except ClientError:
        return False
    return True


async def test_a_receipt_is_scanned_stripped_and_shared_with_the_trip(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, live_settings: Settings
) -> None:
    ann, bea, dan = trip.people["Ann"], trip.members["Bea"], trip.members["Dan"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(2_000, ann, [ann]))
    _, cursor, _ = await pull_all(api, dan, f"plan:{trip.plan_id}")

    original = photo_with_location()
    scanning = await upload(api, bea, trip, original, expense_id=expense["id"])
    assert (scanning["state"], scanning["expense_id"]) == ("scanning", expense["id"])
    assert stored(live_settings, f"incoming/{scanning['id']}")
    assert await tasks.process_media_upload.func(scanning["id"]) == "ready"
    assert not stored(live_settings, f"incoming/{scanning['id']}")

    path = trip.path(f"/media/{scanning['id']}")
    signed = await ok(await api.post(f"{path}/download-url", headers=dan.headers))
    async with httpx.AsyncClient() as storage:
        fetched = await storage.get(signed["url"])
    assert fetched.status_code == 200
    with Image.open(io.BytesIO(fetched.content)) as image:
        assert image.format == "JPEG" and image.size == (64, 48)
        assert dict(image.getexif()) == {}
        assert "exif" not in image.info
    assert len(fetched.content) != len(original)

    items, _, _ = await pull_all(api, dan, f"plan:{trip.plan_id}", cursor)
    synced = [i for i in items if i["entity_type"] == "media"]
    assert synced[-1]["data"]["state"] == "ready"
    assert synced[-1]["data"]["content_type"] == "image/jpeg"
    assert "url" not in synced[-1]["data"]
    # Once processed, it is not uploaded or scanned again.
    assert (await api.post(f"{path}/uploaded", headers=bea.headers)).status_code == 409
    assert (await api.post(f"{path}/upload-url", headers=bea.headers)).status_code == 409


async def test_unsafe_or_wrong_files_never_become_usable(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    live_settings: Settings,
) -> None:
    ann, bea = trip.people["Ann"], trip.members["Bea"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(500, ann, [ann]))

    async def settle(data: bytes, content_type: str = "image/jpeg") -> dict[str, Any]:
        sent = await upload(
            api, bea, trip, data, content_type=content_type, expense_id=expense["id"]
        )
        await tasks.process_media_upload.func(sent["id"])
        listed = await ok(await api.get(trip.path("/media"), headers=bea.headers))
        found: dict[str, Any] = next(row for row in listed if row["id"] == sent["id"])
        return found

    infected = await settle(b"%PDF-1.4\n" + EICAR, "application/pdf")
    assert (infected["state"], infected["rejection"]) == ("rejected", "malware")
    assert not stored(live_settings, f"incoming/{infected['id']}")
    assert not stored(live_settings, f"media/{infected['id']}")
    disguised = await settle(b"MZ" + b"\0" * 200)  # an executable sent as a JPEG
    assert (disguised["state"], disguised["rejection"]) == ("rejected", "type")
    broken = await settle(b"\xff\xd8\xff\xe0" + b"\0" * 300)
    assert (broken["state"], broken["rejection"]) == ("rejected", "unreadable")
    pdf = await settle(b"%PDF-1.4\n%%EOF\n", "application/pdf")
    assert (pdf["state"], pdf["content_type"]) == ("ready", "application/pdf")
    assert (
        await api.post(trip.path(f"/media/{infected['id']}/download-url"), headers=bea.headers)
    ).status_code == 409

    # Reported uploaded before anything reached storage.
    early = await ok(
        await api.post(
            trip.path("/media"),
            json={
                "kind": "receipt",
                "content_type": "image/png",
                "size_bytes": 10,
                "expense_id": expense["id"],
            },
            headers=bea.headers,
        ),
        201,
    )
    await ok(await api.post(trip.path(f"/media/{early['id']}/uploaded"), headers=bea.headers))
    assert await tasks.process_media_upload.func(early["id"]) == "rejected"

    refused = [
        ({"kind": "receipt", "content_type": "image/png", "size_bytes": 10}, 422),
        ({"kind": "cover", "content_type": "image/png", "size_bytes": 10}, 403),
        (
            {
                "kind": "receipt",
                "content_type": "image/png",
                "size_bytes": 10**9,
                "expense_id": expense["id"],
            },
            422,
        ),
    ]
    for body, expected in refused:
        response = await api.post(trip.path("/media"), json=body, headers=bea.headers)
        assert response.status_code == expected, (body, response.text)
    pdf_cover = await api.post(
        trip.path("/media"),
        json={"kind": "cover", "content_type": "application/pdf", "size_bytes": 10},
        headers=trip.owner.headers,
    )
    assert pdf_cover.status_code == 422
    stranger = await sign_in(api, identity_provider, name="Stranger")
    assert (
        await api.post(trip.path(f"/media/{pdf['id']}/download-url"), headers=stranger.headers)
    ).status_code == 404
    # A broken image is refused once, never retried for days.
    corrupt = await settle(b"RIFF\x40\x00\x00\x00WEBPVP8X" + b"\x01" * 60)
    assert (corrupt["state"], corrupt["rejection"]) == ("rejected", "unreadable")
    listed = await ok(await api.get(trip.path("/media"), headers=trip.owner.headers))
    assert {pdf["id"], infected["id"]} <= {row["id"] for row in listed}


async def test_covers_albums_and_deleting_files(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    worker: Runtime,
    live_settings: Settings,
    admin: AdminDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, bea, dan = trip.owner, trip.members["Bea"], trip.members["Dan"]
    cover = await upload(api, owner, trip, photo_with_location(), kind="cover")
    _, cursor, _ = await pull_all(api, dan, f"plan:{trip.plan_id}")
    plan_version = (await ok(await api.get(trip.path(), headers=owner.headers)))["version"]
    early = await api.patch(
        trip.path(), json={"cover_media_id": cover["id"]}, headers=if_match(plan_version, owner)
    )
    assert early.status_code == 422  # not ready yet
    await tasks.process_media_upload.func(cover["id"])
    plan = await ok(
        await api.patch(
            trip.path(),
            json={"cover_media_id": cover["id"], "album_url": "https://photos.app.goo.gl/abc"},
            headers=if_match(plan_version, owner),
        )
    )
    assert (plan["cover_media_id"], plan["album_url"]) == (
        cover["id"],
        "https://photos.app.goo.gl/abc",
    )
    bad_album = await api.patch(
        trip.path(),
        json={"album_url": "http://x.example"},
        headers=if_match(plan["version"], owner),
    )
    assert bad_album.status_code == 422
    assert (
        await api.post(
            trip.path("/media"),
            json={"kind": "cover", "content_type": "image/jpeg", "size_bytes": 10},
            headers=bea.headers,
        )
    ).status_code == 403
    hangout = await ok(
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Karaoke", "base_currency": "VND"},
            headers=owner.headers,
        ),
        201,
    )
    no_cover = await api.post(
        f"/v1/plans/{hangout['id']}/media",
        json={"kind": "cover", "content_type": "image/jpeg", "size_bytes": 10},
        headers=owner.headers,
    )
    assert no_cover.status_code == 409

    # Deleting: the uploader or an organiser, never someone else; storage follows.
    ann = trip.people["Ann"]
    expense = await add_expense(api, owner, trip, equal_expense(700, ann, [ann]))
    receipt = await upload(api, bea, trip, photo_with_location(), expense_id=expense["id"])
    await tasks.process_media_upload.func(receipt["id"])
    receipt_path = trip.path(f"/media/{receipt['id']}")
    assert (await api.delete(receipt_path, headers=dan.headers)).status_code == 403
    assert (await api.delete(receipt_path, headers=bea.headers)).status_code == 204
    assert (
        await api.delete(trip.path(f"/media/{cover['id']}"), headers=owner.headers)
    ).status_code == 204
    cleared = await ok(await api.get(trip.path(), headers=owner.headers))
    assert cleared["cover_media_id"] is None
    items, _, _ = await pull_all(api, dan, f"plan:{trip.plan_id}", cursor)
    plans = [i["data"] for i in items if i["entity_type"] == "plan"]
    assert plans[-1]["cover_media_id"] is None and plans[-1]["version"] == cleared["version"]
    assert stored(live_settings, f"media/{receipt['id']}")
    assert await tasks.delete_media_objects.func(0) == 2
    assert not stored(live_settings, f"media/{receipt['id']}")
    assert not stored(live_settings, f"media/{cover['id']}")
    # The incoming keys stay queued a while, in case an upload URL is used again.
    assert (
        admin.scalar(
            "SELECT count(*) FROM media_memories.object_deletions WHERE requested_at > now()"
        )
        == 2
    )
    assert [
        row["id"] for row in await ok(await api.get(trip.path("/media"), headers=owner.headers))
    ] == []

    # A receipt limit, once paid plans set one.
    monkeypatch.setattr(live_settings, "media_receipts_per_plan", 1)
    body = {
        "kind": "receipt",
        "content_type": "image/png",
        "size_bytes": 10,
        "expense_id": expense["id"],
    }
    await ok(await api.post(trip.path("/media"), json=body, headers=bea.headers), 201)
    limited = await api.post(trip.path("/media"), json=body, headers=bea.headers)
    assert limited.status_code == 409 and limited.json()["code"] == "MEDIA_LIMIT_REACHED"


async def test_the_worker_leaves_nothing_behind_and_waits_out_a_scanner_outage(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    worker: Runtime,
    live_settings: Settings,
    admin: AdminDatabase,
) -> None:
    ann, bea = trip.people["Ann"], trip.members["Bea"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(300, ann, [ann]))

    # Deleted while it was being scanned: the clean copy is queued for deletion too.
    gone = await upload(api, bea, trip, photo_with_location(), expense_id=expense["id"])
    admin.execute(
        "UPDATE media_memories.media SET deleted_at = now(), version = version + 1 WHERE id = %s",
        gone["id"],
    )
    assert await tasks.process_media_upload.func(gone["id"]) == "scanning"
    assert await tasks.delete_media_objects.func(0) >= 1
    assert not stored(live_settings, f"media/{gone['id']}")
    assert not stored(live_settings, f"incoming/{gone['id']}")

    # The scanner is down: the job fails (and retries), the file waits, unserved.
    waiting = await upload(api, bea, trip, photo_with_location(), expense_id=expense["id"])
    worker.__dict__["scanner"] = ClamdScanner("127.0.0.1", 9)  # nothing listens there
    with pytest.raises(ScannerUnavailable):
        await tasks.process_media_upload.func(waiting["id"])
    assert (
        admin.scalar("SELECT state FROM media_memories.media WHERE id = %s", waiting["id"])
        == "scanning"
    )
    assert stored(live_settings, f"incoming/{waiting['id']}")
    del worker.__dict__["scanner"]

    # The hourly sweep re-queues it, and forgets uploads abandoned for a week.
    abandoned = await ok(
        await api.post(
            trip.path("/media"),
            json={
                "kind": "receipt",
                "content_type": "image/png",
                "size_bytes": 10,
                "expense_id": expense["id"],
            },
            headers=bea.headers,
        ),
        201,
    )
    admin.execute(
        "UPDATE media_memories.media SET updated_at = now() - interval '8 days' "
        "WHERE id IN (%s, %s)",
        waiting["id"],
        abandoned["id"],
    )
    assert await tasks.sweep_media_uploads.func(0) == 2
    assert (
        admin.scalar(
            "SELECT count(*) FROM jobs.procrastinate_jobs WHERE task_name = 'media.process' "
            "AND args ->> 'media_id' = %s AND status = 'todo'",
            waiting["id"],
        )
        >= 1
    )
    assert (
        admin.scalar(
            "SELECT count(*) FROM media_memories.object_deletions WHERE object_key = %s",
            f"incoming/{abandoned['id']}",
        )
        == 1
    )
    assert await tasks.process_media_upload.func(waiting["id"]) == "ready"


async def test_the_database_keeps_files_honest(
    api: httpx.AsyncClient, trip: FinancePlan, live_settings: Settings
) -> None:
    ann, bea, dan = trip.people["Ann"], trip.members["Bea"], trip.members["Dan"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(300, ann, [ann]))
    media = await ok(
        await api.post(
            trip.path("/media"),
            json={
                "kind": "receipt",
                "content_type": "image/png",
                "size_bytes": 10,
                "expense_id": expense["id"],
            },
            headers=bea.headers,
        ),
        201,
    )
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    attempts = [
        # The API marking a file ready (only the worker settles files).
        (
            bea.user_id,
            "UPDATE media_memories.media SET state = 'ready', content_type = 'image/png', "
            "size_bytes = 10, version = version + 1 WHERE id = %s",
            (media["id"],),
        ),
        # Someone else reporting the upload, or deleting it.
        (
            dan.user_id,
            "UPDATE media_memories.media SET state = 'scanning', version = version + 1 "
            "WHERE id = %s",
            (media["id"],),
        ),
        (
            dan.user_id,
            "UPDATE media_memories.media SET deleted_at = now(), version = version + 1 "
            "WHERE id = %s",
            (media["id"],),
        ),
        # Queuing another file's objects for deletion.
        (
            bea.user_id,
            "INSERT INTO media_memories.object_deletions (object_key, requested_at) "
            "VALUES (%s, now())",
            (f"media/{media['id']}",),
        ),
    ]
    with psycopg.connect(dsn) as connection:
        for actor, statement, params in attempts:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor,))
                connection.execute(statement, params)
