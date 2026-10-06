"""Push batches: per-operation results, ordering, dependencies, replay, and limits."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.modules.iam import rate_limits
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


def op(
    command: str,
    *,
    operation_id: UUID | None = None,
    target: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    expected_version: int | None = None,
    depends_on: list[UUID] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "operation_id": str(operation_id or new_id()),
        "command": command,
        "schema_version": schema_version,
        "target": target or {},
        "expected_version": expected_version,
        "depends_on": [str(dep) for dep in depends_on or []],
        "payload": payload or {},
        "client_created_at": "2026-10-06T08:00:00Z",
    }


async def push(
    api: httpx.AsyncClient, user: SignedIn, operations: list[dict[str, Any]], **extra: Any
) -> list[dict[str, Any]]:
    response = await api.post(
        "/v1/sync/push",
        json={"device_id": "device-1", "operations": operations, **extra},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


def outcomes(results: list[dict[str, Any]]) -> list[str]:
    return [result["outcome"] for result in results]


async def make_plan(api: httpx.AsyncClient, owner: SignedIn, **body: Any) -> dict[str, Any]:
    response = await api.post(
        "/v1/plans",
        json={"type": "hangout", "title": "Dinner", "base_currency": "USD", **body},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_batch_applies_in_order_and_replays_for_free(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan_id = new_id()
    create_id, update_id, rsvp_id = new_id(), new_id(), new_id()
    batch = [
        op(
            "plan.create",
            operation_id=create_id,
            payload={
                "id": str(plan_id),
                "type": "hangout",
                "title": "Dinner",
                "base_currency": "USD",
            },
        ),
        op(
            "plan.update",
            operation_id=update_id,
            target={"plan_id": str(plan_id)},
            expected_version=1,
            payload={"title": "Late dinner"},
            depends_on=[create_id],
        ),
        op(
            "plan.rsvp",
            operation_id=rsvp_id,
            target={"plan_id": str(plan_id)},
            payload={"status": "going"},
            depends_on=[create_id],
        ),
    ]

    results = await push(api, owner, batch)

    assert outcomes(results) == ["applied", "applied", "applied"]
    assert [result["status"] for result in results] == [201, 200, 200]
    assert results[0]["body"]["id"] == str(plan_id) and results[0]["version"] == 1
    assert results[1]["body"]["title"] == "Late dinner" and results[1]["version"] == 2
    assert results[2]["body"]["rsvp_status"] == "going"
    assert admin.fetch(
        "SELECT source, device_id, idempotency_key FROM sync_audit.operations ORDER BY created_at"
    ) == [
        ("push", "device-1", str(create_id)),
        ("push", "device-1", str(update_id)),
        ("push", "device-1", str(rsvp_id)),
    ]
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.change_log "
            "WHERE scope_id = %s AND operation_id IS NOT NULL",
            plan_id,
        )
        == 4
    )

    # Lost response: the whole batch is resent and nothing runs twice.
    replayed = await push(api, owner, batch)
    assert outcomes(replayed) == ["replayed", "replayed", "replayed"]
    assert [result["body"] for result in replayed] == [result["body"] for result in results]
    assert admin.scalar("SELECT count(*) FROM plans.plans") == 1
    assert admin.scalar("SELECT version FROM plans.plans") == 2


async def test_failures_are_isolated_and_dependencies_skip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    other = await make_plan(api, owner, title="Other")
    bad_id = new_id()
    batch = [
        op(
            "plan.update",
            operation_id=bad_id,
            target={"plan_id": plan["id"]},
            expected_version=1,
            payload={"title": ""},
        ),
        op(
            "plan.rsvp",
            target={"plan_id": plan["id"]},
            payload={"status": "maybe"},
            depends_on=[bad_id],
        ),
        op("plan.rsvp", target={"plan_id": plan["id"]}, payload={"status": "going"}),
        op(
            "plan.update",
            target={"plan_id": other["id"]},
            expected_version=1,
            payload={"title": "Fine"},
        ),
        op(
            "plan.update",
            target={"plan_id": other["id"]},
            expected_version=1,
            payload={"title": "Stale"},
        ),
        op(
            "plan.update",
            target={"plan_id": other["id"]},
            expected_version=2,
            payload={"title": "Unknown field", "nope": 1},
        ),
        op("plan.rsvp", target={"plan_id": str(uuid4())}, payload={"status": "going"}),
        op("plan.sing", target={"plan_id": plan["id"]}),
        op(
            "plan.rsvp",
            target={"plan_id": plan["id"]},
            payload={"status": "going"},
            schema_version=2,
        ),
        op("plan.update", target={"plan_id": plan["id"]}, payload={"title": "No version"}),
        op("plan.update", target={}, expected_version=1, payload={"title": "No target"}),
    ]

    results = await push(api, owner, batch)

    assert outcomes(results) == [
        "rejected",  # blank title
        "skipped",  # depends on the rejected one
        "applied",
        "applied",
        "conflict",  # stale version
        "rejected",  # unknown payload field
        "rejected",  # unknown plan
        "rejected",  # unknown command
        "upgrade_required",
        "rejected",  # versioned command without expected_version
        "rejected",  # missing target
    ]
    assert results[0]["problem"]["code"] == "VALIDATION_FAILED"
    assert results[0]["problem"]["details"] and "title" in str(results[0]["problem"]["details"])
    assert results[1]["problem"]["code"] == "OPERATION_SKIPPED"
    assert results[4]["problem"]["code"] == "VERSION_CONFLICT"
    assert results[4]["problem"]["current"]["title"] == "Fine"
    assert results[5]["problem"]["details"] == {"nope": "Extra inputs are not permitted"}
    assert results[6]["problem"]["code"] == "NOT_FOUND"
    assert results[7]["problem"]["code"] == "VALIDATION_FAILED"
    assert results[8]["problem"]["code"] == "CLIENT_UPGRADE_REQUIRED"
    assert results[9]["problem"]["code"] == "PRECONDITION_REQUIRED"
    assert "plan_id" in results[10]["problem"]["detail"]
    assert admin.scalar("SELECT count(*) FROM sync_audit.operations") == 2
    assert admin.fetch("SELECT title FROM plans.plans ORDER BY title") == [("Dinner",), ("Fine",)]


async def test_transient_failures_block_later_operations_on_the_same_scope(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    limit = rate_limits.DUPLICATION_PER_USER.limit
    batch = [
        op(
            "plan.duplicate",
            target={"plan_id": plan["id"]},
            payload={"timing": {"mode": "undecided"}},
        )
        for _ in range(limit + 1)
    ]
    batch.append(op("plan.rsvp", target={"plan_id": plan["id"]}, payload={"status": "going"}))
    # A new plan addresses no existing plan, so it is not held back.
    batch.append(
        op("plan.create", payload={"type": "hangout", "title": "Other", "base_currency": "USD"})
    )

    results = await push(api, owner, batch)

    assert outcomes(results) == ["applied"] * limit + ["retry", "skipped", "applied"]
    limited = results[limit]["problem"]
    assert limited["code"] == "RATE_LIMITED" and limited["retry_after_seconds"] >= 1
    assert admin.scalar("SELECT count(*) FROM plans.plans") == limit + 2

    # Replaying the applied duplicates does not consume the limit again.
    replayed = await push(api, owner, results and batch[:limit])
    assert outcomes(replayed) == ["replayed"] * limit


async def test_dependencies_on_earlier_batches_and_unknown_operations(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan_id, create_id = new_id(), new_id()
    first = await push(
        api,
        owner,
        [
            op(
                "plan.create",
                operation_id=create_id,
                payload={
                    "id": str(plan_id),
                    "type": "hangout",
                    "title": "Dinner",
                    "base_currency": "USD",
                },
            )
        ],
    )
    assert outcomes(first) == ["applied"]

    second = await push(
        api,
        owner,
        [
            op(
                "plan.rsvp",
                target={"plan_id": str(plan_id)},
                payload={"status": "going"},
                depends_on=[create_id],
            ),
            op(
                "plan.rsvp",
                target={"plan_id": str(plan_id)},
                payload={"status": "maybe"},
                depends_on=[uuid4()],
            ),
        ],
    )
    assert outcomes(second) == ["applied", "skipped"]
    assert "was not applied" in second[1]["problem"]["detail"]


async def test_removed_participant_cannot_push_queued_changes(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    member = await sign_in(api, identity_provider, name="Member")
    plan = await make_plan(api, owner)
    invite = await api.post(
        f"/v1/plans/{plan['id']}/invites", json={"role": "admin"}, headers=owner.headers
    )
    joined = await api.post(
        "/v1/invites/redeem", json={"token": invite.json()["token"]}, headers=member.headers
    )
    assert joined.status_code == 200, joined.text
    participant_id = joined.json()["participant"]["id"]
    removed = await api.delete(
        f"/v1/plans/{plan['id']}/participants/{participant_id}", headers=owner.headers
    )
    assert removed.status_code == 204

    results = await push(
        api,
        member,
        [
            op(
                "plan.update",
                target={"plan_id": plan["id"]},
                expected_version=1,
                payload={"title": "Hijacked"},
            ),
            op("plan.rsvp", target={"plan_id": plan["id"]}, payload={"status": "going"}),
        ],
    )

    assert outcomes(results) == ["rejected", "rejected"]
    assert {result["problem"]["code"] for result in results} == {"NOT_FOUND"}
    assert admin.scalar("SELECT title FROM plans.plans") == "Dinner"
    assert admin.scalar("SELECT count(*) FROM sync_audit.operations") == 0


async def test_batch_level_validation_and_limits(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    duplicate = new_id()
    repeated = await api.post(
        "/v1/sync/push",
        json={
            "operations": [
                op(
                    "plan.create",
                    operation_id=duplicate,
                    payload={"type": "hangout", "title": "A", "base_currency": "USD"},
                ),
                op(
                    "plan.create",
                    operation_id=duplicate,
                    payload={"type": "hangout", "title": "B", "base_currency": "USD"},
                ),
            ]
        },
        headers=owner.headers,
    )
    assert repeated.status_code == 422
    too_many = await api.post(
        "/v1/sync/push",
        json={
            "operations": [op("plan.rsvp", target={"plan_id": str(uuid4())}) for _ in range(101)]
        },
        headers=owner.headers,
    )
    assert too_many.status_code == 422
    outdated = await api.post(
        "/v1/sync/push",
        json={"protocol_version": 7, "operations": [op("plan.rsvp")]},
        headers=owner.headers,
    )
    assert outdated.status_code == 426
    empty = await api.post("/v1/sync/push", json={"operations": []}, headers=owner.headers)
    assert empty.status_code == 422
    oversized = await api.post(
        "/v1/sync/push",
        content=b"{}",
        headers={**owner.headers, "Content-Type": "application/json", "Content-Length": "2000000"},
    )
    assert oversized.status_code == 413


async def test_push_kill_switches(
    live_settings: Settings, identity_provider: IdentityProviderStub
) -> None:
    from beluno.api.main import create_app
    from beluno.db.session import Database

    settings = live_settings.model_copy(
        update={"sync_push_enabled": False, "sync_disabled_commands": ["plan.rsvp"]}
    )
    database = Database(settings)
    app = create_app(
        settings=settings, database=database, identity_verifier=identity_provider.verifier(settings)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as api:
        owner = await sign_in(api, identity_provider, name="Owner")
        refused = await api.post(
            "/v1/sync/push",
            json={"operations": [op("plan.rsvp", target={"plan_id": str(uuid4())})]},
            headers=owner.headers,
        )
        shake = await api.post(
            "/v1/sync/handshake", json={"protocol_version": 1}, headers=owner.headers
        )
    await database.close()
    assert refused.status_code == 503 and refused.json()["code"] == "FEATURE_DISABLED"
    assert shake.json()["features"] == {
        "push_enabled": False,
        "pull_enabled": True,
        "disabled_commands": ["plan.rsvp"],
        "finance_writes_enabled": True,
    }


async def test_disabled_command_is_a_transient_push_failure(
    live_settings: Settings, identity_provider: IdentityProviderStub
) -> None:
    from beluno.api.main import create_app
    from beluno.db.session import Database

    settings = live_settings.model_copy(update={"sync_disabled_commands": ["plan.rsvp"]})
    database = Database(settings)
    app = create_app(
        settings=settings, database=database, identity_verifier=identity_provider.verifier(settings)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as api:
        owner = await sign_in(api, identity_provider, name="Owner")
        plan = await make_plan(api, owner)
        results = await push(
            api,
            owner,
            [
                op("plan.rsvp", target={"plan_id": plan["id"]}, payload={"status": "going"}),
                op(
                    "plan.update",
                    target={"plan_id": plan["id"]},
                    expected_version=1,
                    payload={"title": "Blocked behind the disabled command"},
                ),
            ],
        )
    await database.close()
    assert outcomes(results) == ["retry", "skipped"]
    assert results[0]["problem"]["code"] == "FEATURE_DISABLED"


async def test_an_operation_id_cannot_name_two_commands(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    owner = await sign_in(api, identity_provider, name="Owner")
    plan = await make_plan(api, owner)
    shared = new_id()
    rsvp = op(
        "plan.rsvp",
        operation_id=shared,
        target={"plan_id": plan["id"]},
        payload={"status": "going"},
    )
    assert outcomes(await push(api, owner, [rsvp])) == ["applied"]

    reused = await push(
        api,
        owner,
        [
            op(
                "plan.update",
                operation_id=shared,
                target={"plan_id": plan["id"]},
                expected_version=1,
                payload={"title": "Reused id"},
            )
        ],
    )
    assert outcomes(reused) == ["conflict"]
    assert reused[0]["problem"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert outcomes(await push(api, owner, [rsvp])) == ["replayed"]
    assert admin.scalar("SELECT title FROM plans.plans") == "Dinner"
