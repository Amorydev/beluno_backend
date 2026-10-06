"""The API records command, sync, and change-log metrics while serving real traffic."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from beluno.api.main import create_app
from beluno.config import Settings
from beluno.db.session import Database
from beluno.observability import metrics
from beluno.testkit.api_client import sign_in
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


@pytest.fixture
async def metered_api(
    live_settings: Settings, identity_provider: IdentityProviderStub
) -> AsyncIterator[tuple[httpx.AsyncClient, InMemoryMetricReader]]:
    reader = InMemoryMetricReader()
    metrics.use_meter_provider(MeterProvider(metric_readers=[reader]))
    database = Database(live_settings)
    app = create_app(
        settings=live_settings,
        database=database,
        identity_verifier=identity_provider.verifier(live_settings),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client, reader
    finally:
        await database.close()
        metrics.use_meter_provider(None)


def points(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, Any], ...], float]:
    data = reader.get_metrics_data()
    assert data is not None
    found = {}
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        value = getattr(point, "value", None)
                        found[tuple(sorted(point.attributes.items()))] = (
                            point.sum if value is None else value
                        )
    return found


async def test_commands_sync_and_changes_are_measured(
    metered_api: tuple[httpx.AsyncClient, InMemoryMetricReader],
    identity_provider: IdentityProviderStub,
) -> None:
    api, reader = metered_api
    owner = await sign_in(api, identity_provider, name="Owner")
    headers = {**owner.headers, "Idempotency-Key": "k"}
    body = {"type": "hangout", "title": "Dinner", "base_currency": "USD"}
    created = await api.post("/v1/plans", json=body, headers=headers)
    replayed = await api.post("/v1/plans", json=body, headers=headers)
    assert created.status_code == replayed.status_code == 201
    stale = await api.patch(
        f"/v1/plans/{created.json()['id']}",
        json={"title": "x"},
        headers={**owner.headers, "If-Match": '"9"'},
    )
    assert stale.status_code == 412
    pulled = await api.post(
        "/v1/sync/pull",
        json={"scopes": [{"scope": f"plan:{created.json()['id']}", "cursor": None}]},
        headers=owner.headers,
    )
    assert pulled.status_code == 200
    pushed = await api.post(
        "/v1/sync/push",
        json={
            "operations": [
                {
                    "operation_id": "01a10000-0000-7000-8000-000000000001",
                    "command": "plan.rsvp",
                    "target": {"plan_id": created.json()["id"]},
                    "payload": {"status": "going"},
                }
            ]
        },
        headers=owner.headers,
    )
    assert pushed.status_code == 200

    commands = points(reader, "beluno.commands")
    assert commands[(("command", "plan.create"), ("outcome", "applied"), ("source", "http"))] == 1
    assert commands[(("command", "plan.create"), ("outcome", "replayed"), ("source", "http"))] == 1
    assert (
        commands[(("command", "plan.update"), ("outcome", "version_conflict"), ("source", "http"))]
        == 1
    )
    assert commands[(("command", "plan.rsvp"), ("outcome", "applied"), ("source", "push"))] == 1
    assert points(reader, "beluno.sync.push.results") == {(("outcome", "applied"),): 1}
    assert points(reader, "beluno.sync.pull.pages") == {
        (("scope_type", "plan"), ("status", "ok")): 1
    }
    assert points(reader, "beluno.sync.pull.items")[(("scope_type", "plan"),)] == 2
    assert points(reader, "beluno.sync.changes.appended")[()] >= 6
    assert (("command", "plan.create"),) in points(reader, "beluno.command.duration")
