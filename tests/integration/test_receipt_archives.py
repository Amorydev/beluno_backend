"""Every receipt of a trip in one zip, for the person who asked."""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fpdf import FPDF

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.receipt_archives import fail_stale_archives
from beluno.storage import ObjectStorage
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.media import photo_with_location, upload
from beluno.testkit.stores import pass_for
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
    return await finance_plan(api, identity_provider, admin, members=("Bea",))


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def small_pdf() -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(text="Receipt 42")
    return bytes(pdf.output())


async def receipts(api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime) -> dict[str, Any]:
    owner, ann = trip.owner, trip.people["Ann"]
    expense = await add_expense(
        api, owner, trip, equal_expense(12_000, ann, [ann], description="=Phở Hà Nội")
    )
    for data, content_type in (
        (photo_with_location(), "image/jpeg"),
        (small_pdf(), "application/pdf"),
    ):
        media = await upload(
            api, owner, trip, data, content_type=content_type, expense_id=expense["id"]
        )
        assert await tasks.process_media_upload.func(media["id"]) == "ready"
    return expense


async def test_a_zip_of_every_receipt_for_whoever_asked(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    expense = await receipts(api, trip, worker)
    path = trip.path("/receipt-archives")
    refused = await api.post(path, headers=owner.headers)
    assert refused.status_code == 403 and refused.json()["code"] == "UPGRADE_REQUIRED"
    pass_for(admin, trip)

    asked = await ok(await api.post(path, headers=owner.headers), 202)
    assert asked["state"] == "pending" and asked["download_url"] is None
    again = await ok(await api.post(path, headers=owner.headers), 202)
    assert again["id"] == asked["id"]  # still being built: the same one
    assert await tasks.build_receipt_archive.func(asked["id"]) == "ready"
    assert await tasks.build_receipt_archive.func(asked["id"]) == "skipped"
    # Ready and nothing new since: the same zip again.
    same = await ok(await api.post(path, headers=owner.headers), 202)
    assert same["id"] == asked["id"] and same["state"] == "ready"

    ready = await ok(await api.get(f"{path}/{asked['id']}", headers=owner.headers))
    assert ready["state"] == "ready" and ready["receipts"] == 2 and ready["download_url"]
    async with httpx.AsyncClient() as storage:
        download = await storage.get(ready["download_url"])
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = sorted(archive.namelist())
    assert names[-1] == "receipts.csv" and len(names) == 3
    assert names[0].endswith(".jpg") and names[1].endswith(".pdf")
    assert "Phở-Hà-Nội" in names[0]
    index = list(csv.DictReader(io.StringIO(archive.read("receipts.csv").decode("utf-8-sig"))))
    assert [row["expense_id"] for row in index] == [expense["id"]] * 2
    assert index[0]["description"] == "'=Phở Hà Nội" and index[0]["amount"] == "120.00"

    # Another member cannot see it; the file is queued to go a day later.
    hidden = await api.get(f"{path}/{asked['id']}", headers=bea.headers)
    assert hidden.status_code == 404
    [(due,)] = admin.fetch(
        "SELECT requested_at - %s::timestamptz FROM media_memories.object_deletions"
        " WHERE object_key = %s",
        ready["ready_at"],
        f"archives/{asked['id']}.zip",
    )
    assert due.total_seconds() == 24 * 3600

    admin.execute(
        "UPDATE media_memories.receipt_archives SET expires_at = now() - interval '1 minute'"
        " WHERE id = %s",
        asked["id"],
    )
    gone = await ok(await api.get(f"{path}/{asked['id']}", headers=owner.headers))
    assert gone["state"] == "expired" and gone["download_url"] is None
    # A new request makes a new archive.
    fresh = await ok(await api.post(path, headers=owner.headers), 202)
    assert fresh["id"] != asked["id"]


async def test_no_receipts_no_archive(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    pass_for(admin, trip)
    empty = await api.post(trip.path("/receipt-archives"), headers=trip.owner.headers)
    assert empty.status_code == 409 and empty.json()["code"] == "NO_RECEIPTS"


async def test_voided_expenses_stay_out_and_new_receipts_make_a_new_zip(
    api: httpx.AsyncClient, trip: FinancePlan, worker: Runtime, admin: AdminDatabase
) -> None:
    owner = trip.owner
    pass_for(admin, trip)
    kept = await receipts(api, trip, worker)
    voided = await receipts(api, trip, worker)
    await ok(
        await api.post(trip.path(f"/expenses/{voided['id']}/void"), headers=if_match(1, owner))
    )
    path = trip.path("/receipt-archives")
    first = await ok(await api.post(path, headers=owner.headers), 202)
    assert await tasks.build_receipt_archive.func(first["id"]) == "ready"
    ready = await ok(await api.get(f"{path}/{first['id']}", headers=owner.headers))
    assert ready["receipts"] == 2
    async with httpx.AsyncClient() as storage:
        listed = zipfile.ZipFile(io.BytesIO((await storage.get(ready["download_url"])).content))
    index = list(csv.DictReader(io.StringIO(listed.read("receipts.csv").decode("utf-8-sig"))))
    assert {row["expense_id"] for row in index} == {kept["id"]}
    await receipts(api, trip, worker)
    second = await ok(await api.post(path, headers=owner.headers), 202)
    assert second["id"] != first["id"]


async def test_failures_never_leave_an_archive_stuck(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = trip.owner
    pass_for(admin, trip)
    await receipts(api, trip, worker)
    path = trip.path("/receipt-archives")

    async def broken(*_: object, **__: object) -> None:
        raise OSError("storage is down")

    monkeypatch.setattr(ObjectStorage, "write_file", broken)
    asked = await ok(await api.post(path, headers=owner.headers), 202)
    assert await tasks.build_receipt_archive.func(asked["id"]) == "failed"
    failed = await ok(await api.get(f"{path}/{asked['id']}", headers=owner.headers))
    assert failed["failure"] == "storage"

    # A build cut short by a crash: the hourly sweep fails it, and asking again works.
    crashed = await ok(await api.post(path, headers=owner.headers), 202)
    assert crashed["id"] != asked["id"]
    admin.execute(
        "UPDATE media_memories.receipt_archives SET state = 'building',"
        " created_at = now() - interval '2 hours' WHERE id = %s",
        crashed["id"],
    )
    stuck = await ok(await api.post(path, headers=owner.headers), 202)
    assert stuck["id"] == crashed["id"]  # until the sweep runs, it is the one in the making
    assert await fail_stale_archives(worker) == 1
    swept = await ok(await api.get(f"{path}/{crashed['id']}", headers=owner.headers))
    assert (swept["state"], swept["failure"]) == ("failed", "storage")
    assert (
        admin.scalar(
            "SELECT count(*) FROM media_memories.object_deletions WHERE object_key = %s",
            f"archives/{crashed['id']}.zip",
        )
        == 1
    )
    assert (await ok(await api.post(path, headers=owner.headers), 202))["id"] != crashed["id"]


async def test_too_large_fails_before_reading_anything(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
    live_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass_for(admin, trip)
    await receipts(api, trip, worker)
    monkeypatch.setattr(live_settings, "receipt_archive_max_bytes", 1024)

    async def unread(*_: object, **__: object) -> None:
        raise AssertionError("nothing is read for an archive that is too large")

    monkeypatch.setattr(ObjectStorage, "read", unread)
    asked = await ok(
        await api.post(trip.path("/receipt-archives"), headers=trip.owner.headers), 202
    )
    assert await tasks.build_receipt_archive.func(asked["id"]) == "failed"


async def test_hangouts_are_free_and_a_purged_request_takes_its_zip(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    hangout = await finance_plan(api, identity_provider, admin, plan_type="hangout")
    await receipts(api, hangout, worker)
    asked = await ok(
        await api.post(hangout.path("/receipt-archives"), headers=hangout.owner.headers), 202
    )
    assert await tasks.build_receipt_archive.func(asked["id"]) == "ready"
    admin.execute("DELETE FROM media_memories.receipt_archives WHERE id = %s", asked["id"])
    [(due,)] = admin.fetch(
        "SELECT now() - requested_at FROM media_memories.object_deletions WHERE object_key = %s",
        f"archives/{asked['id']}.zip",
    )
    assert due.total_seconds() < 60  # at once, not a day later
