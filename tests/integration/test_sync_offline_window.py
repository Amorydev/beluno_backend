"""A device that was offline: inside the retention window it catches up, past it it resyncs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.sync_audit.maintenance import compact_changes
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration


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


async def pull(
    api: httpx.AsyncClient, user: SignedIn, scope: str, cursor: str | None
) -> dict[str, Any]:
    response = await api.post(
        "/v1/sync/pull", json={"scopes": [{"scope": scope, "cursor": cursor}]}, headers=user.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["scopes"][0]


async def drain(
    api: httpx.AsyncClient, user: SignedIn, scope: str, cursor: str | None
) -> tuple[list[dict[str, Any]], str]:
    items: list[dict[str, Any]] = []
    while True:
        page = await pull(api, user, scope, cursor)
        assert page["status"] == "ok", page
        items.extend(page["changes"])
        cursor = page["cursor"]
        if not page["has_more"]:
            return items, cursor


async def test_ninety_day_offline_device_catches_up_and_older_cursors_resync(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    worker: Runtime,
    admin: AdminDatabase,
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "v1", "base_currency": "USD"},
            headers=owner.headers,
        )
    ).json()
    scope = f"plan:{plan['id']}"
    _, offline_cursor = await drain(api, owner, scope, None)

    # Ninety days of activity while the device is away: twelve edits, a new person, a
    # budget created and deleted again.
    for version in range(1, 13):
        edited = await api.patch(
            f"/v1/plans/{plan['id']}",
            json={"title": f"v{version + 1}"},
            headers={**owner.headers, "If-Match": f'"{version}"'},
        )
        assert edited.status_code == 200, edited.text
    added = await api.post(
        f"/v1/plans/{plan['id']}/participants",
        json={"placeholder_name": "Grandma"},
        headers=owner.headers,
    )
    assert added.status_code == 201, added.text
    budget = await api.post(
        f"/v1/plans/{plan['id']}/budgets",
        json={"scope": "total", "limit_minor": 10_000},
        headers=owner.headers,
    )
    assert budget.status_code == 201, budget.text
    gone = await api.delete(
        f"/v1/plans/{plan['id']}/budgets/{budget.json()['id']}", headers=owner.headers
    )
    assert gone.status_code == 204, gone.text
    admin.execute("UPDATE sync_audit.change_log SET changed_at = changed_at - interval '95 days'")
    assert await compact_changes(worker) == 0  # everything is still inside retention

    caught_up, cursor = await drain(api, owner, scope, offline_cursor)
    assert [(item["entity_type"], item["operation"]) for item in caught_up] == [
        ("plan", "upsert"),
        ("plan_participant", "upsert"),
        ("ledger", "upsert"),
        ("budget", "delete"),
    ]
    assert caught_up[0]["data"]["title"] == "v13" and caught_up[0]["version"] == 13

    # Past the retention window the old cursor is below the floor and must bootstrap.
    admin.execute("UPDATE sync_audit.change_log SET changed_at = changed_at - interval '100 days'")
    assert await compact_changes(worker) > 0
    assert (await pull(api, owner, scope, offline_cursor))["status"] == "resync_required"
    assert (await pull(api, owner, scope, cursor))["status"] == "ok"
    snapshot, _ = await drain(api, owner, scope, None)
    assert [item["entity_type"] for item in snapshot] == [
        "plan",
        "plan_participant",
        "plan_participant",
        "ledger",
    ]
    assert snapshot[0]["data"]["title"] == "v13"
