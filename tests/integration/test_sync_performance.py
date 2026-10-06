"""Sync-after-connect timing on local PostgreSQL (a smoke bound, not the production SLO).

The measured figures are printed so a run can be recorded; the assertion only
guards against gross regressions on developer hardware.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import httpx
import pytest

from beluno.db.ids import new_id
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

PARTICIPANTS = 100
BOOTSTRAP_RUNS = 10
P95_BUDGET_SECONDS = 3.0


async def drain(api: httpx.AsyncClient, user: SignedIn, scope: str, cursor: str | None) -> int:
    count = 0
    while True:
        response = await api.post(
            "/v1/sync/pull",
            json={"scopes": [{"scope": scope, "cursor": cursor}]},
            headers=user.headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()["scopes"][0]
        assert page["status"] == "ok"
        count += len(page["changes"])
        cursor = page["cursor"]
        if not page["has_more"]:
            return count


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


async def test_bootstrap_and_catch_up_timings(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    seeds = [{"placeholder_name": f"Guest {index}"} for index in range(PARTICIPANTS)]
    plan = (
        await api.post(
            "/v1/plans",
            json={"title": "Big trip", "base_currency": "USD", "participants": seeds},
            headers=owner.headers,
        )
    ).json()
    scope = f"plan:{plan['id']}"
    people: list[dict[str, Any]] = (
        await api.get(f"/v1/plans/{plan['id']}/participants", headers=owner.headers)
    ).json()
    batch = [
        {
            "operation_id": str(new_id()),
            "command": "plan.participant.change_role",
            "target": {"plan_id": plan["id"], "participant_id": person["id"]},
            "expected_version": 1,
            "payload": {"role": "viewer"},
        }
        for person in people
        if person["identity_kind"] == "placeholder"
    ]
    started = time.perf_counter()
    pushed = await api.post("/v1/sync/push", json={"operations": batch}, headers=owner.headers)
    push_seconds = time.perf_counter() - started
    assert pushed.status_code == 200
    assert {result["outcome"] for result in pushed.json()["results"]} == {"applied"}

    bootstrap_samples = []
    for _ in range(BOOTSTRAP_RUNS):
        started = time.perf_counter()
        delivered = await drain(api, owner, scope, None)
        bootstrap_samples.append(time.perf_counter() - started)
        assert delivered == PARTICIPANTS + 2
    print(
        f"\nsync timings (local PostgreSQL, {PARTICIPANTS} participants): "
        f"push {len(batch)} ops {push_seconds:.3f}s; bootstrap p50 "
        f"{statistics.median(bootstrap_samples):.3f}s "
        f"p95 {percentile(bootstrap_samples, 0.95):.3f}s"
    )
    assert percentile(bootstrap_samples, 0.95) < P95_BUDGET_SECONDS
