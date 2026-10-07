"""Problem reports keep the person's words and a diagnostic snapshot with no one's data."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import psycopg
import pytest

from beluno.auth import AccessTokenCodec
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime
from beluno.modules.iam import rate_limits
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.modules.iam.rate_limits import RateLimit
from beluno.modules.support import list_reports
from beluno.testkit.api_client import sign_in, signed_in_from
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import FinancePlan, add_expense, equal_expense, finance_plan
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration

CODE = re.compile(r"^BLN-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin, members=("Bea",))


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def report_body(trip: FinancePlan, expense: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "category": "balance_wrong",
        "plan_id": trip.plan_id,
        "linked": {"entity_type": "expense", "entity_id": expense["id"]},
        "message": "My share is 1 less than Ann's. Is that right?",
        "client": {"app_version": "1.4.0 (212)", "pending_operations": 2},
        **extra,
    }


async def test_a_report_carries_a_code_and_a_snapshot_without_anyones_data(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase, live_settings: Settings
) -> None:
    bea = trip.members["Bea"]
    ann, bea_id = trip.people["Ann"], trip.people["Bea"]
    expense = await add_expense(
        api, trip.owner, trip, equal_expense(2_369, ann, [ann, bea_id], description="Yakiniku")
    )

    check = await ok(
        await api.get(
            "/v1/support/checks",
            params={"plan_id": trip.plan_id, "entity_type": "expense", "entity_id": expense["id"]},
            headers=bea.headers,
        )
    )
    assert check == {
        "plan_id": trip.plan_id,
        "ledger_ok": True,
        "record_found": True,
        "record_version": 1,
    }

    sent = await ok(
        await api.post("/v1/support/reports", json=report_body(trip, expense), headers=bea.headers),
        201,
    )
    assert CODE.match(sent["diagnostic_code"])
    [(category, message, entity_id, diagnostics)] = admin.fetch(
        "SELECT category, message, entity_id::text, diagnostics "
        "FROM analytics_ops.problem_reports WHERE id = %s",
        sent["id"],
    )
    assert (category, entity_id) == ("balance_wrong", expense["id"])
    assert message == "My share is 1 less than Ann's. Is that right?"
    assert "Yakiniku" not in json.dumps(diagnostics) and "Ann" not in json.dumps(diagnostics)
    # Exactly these keys: anything new must be reviewed for privacy first.
    assert set(diagnostics) == {
        "generated_at",
        "server_release",
        "user",
        "session",
        "user_scope_seq",
        "client",
        "plan",
        "record",
    }
    assert set(diagnostics["session"]) == {"id", "auth_method", "platform", "app_version"}
    assert set(diagnostics["plan"]) == {
        "id",
        "type",
        "state",
        "base_currency",
        "role",
        "access_state",
        "scope_seq",
        "ledger_seq",
        "ledger_status",
        "ledger_problems",
    }
    assert diagnostics["plan"]["ledger_problems"] == 0
    assert diagnostics["plan"]["role"] == "member"
    assert diagnostics["record"] == {
        "type": "expense",
        "id": expense["id"],
        "version": 1,
        "state": "active",
        "deleted": False,
    }
    assert diagnostics["client"] == {"app_version": "1.4.0 (212)", "pending_operations": 2}
    assert diagnostics["user"] == {"id": bea.user_id, "guest": False}
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events "
            "WHERE action = 'support.problem_reported' AND entity_id = %s",
            sent["id"],
        )
        == 1
    )
    # The API reads no report back, not even its own.
    assert live_settings.api_database_dsn is not None
    dsn = live_settings.api_database_dsn.replace("postgresql+psycopg://", "postgresql://")
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (bea.user_id,))
        connection.execute("SELECT count(*) FROM analytics_ops.problem_reports")
    with (
        psycopg.connect(dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute("SELECT set_config('app.actor_id', %s, true)", (bea.user_id,))
        connection.execute(
            "INSERT INTO analytics_ops.problem_reports (id, user_id, category, message, "
            "created_at) VALUES (gen_random_uuid(), %s, 'other', 'x', now())",
            (trip.owner.user_id,),
        )

    # A ledger that no longer reconciles shows on the check and in the next snapshot.
    admin.execute(
        "UPDATE finance.account_balances SET balance_minor = balance_minor + 1 "
        "WHERE account_id = (SELECT id FROM finance.ledger_accounts "
        "WHERE plan_id = %s AND participant_id IS NOT NULL LIMIT 1)",
        trip.plan_id,
    )
    drifted = await ok(
        await api.get("/v1/support/checks", params={"plan_id": trip.plan_id}, headers=bea.headers)
    )
    assert (drifted["ledger_ok"], drifted["record_found"]) == (False, None)
    again = await ok(
        await api.post("/v1/support/reports", json=report_body(trip, expense), headers=bea.headers),
        201,
    )
    assert (
        admin.scalar(
            "SELECT (diagnostics -> 'plan' ->> 'ledger_problems')::int "
            "FROM analytics_ops.problem_reports WHERE id = %s",
            again["id"],
        )
        > 0
    )


async def test_reports_stay_within_what_the_person_may_see(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    admin: AdminDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, bea = trip.owner, trip.members["Bea"]
    ann = trip.people["Ann"]
    expense = await add_expense(api, owner, trip, equal_expense(500, ann, [ann]))
    other = await finance_plan(api, identity_provider, admin, members=())
    theirs = await add_expense(
        api, other.owner, other, equal_expense(500, other.people["Ann"], [other.people["Ann"]])
    )
    private = await ok(
        await api.post(
            trip.path("/packing"),
            json={"name": "Pills", "visibility": "private"},
            headers=owner.headers,
        ),
        201,
    )
    refused = [
        # A record of another plan, someone else's private item, or a link with no plan.
        report_body(trip, theirs),
        {
            **report_body(trip, expense),
            "linked": {"entity_type": "packing_item", "entity_id": private["id"]},
        },
        {**report_body(trip, expense), "plan_id": None},
        {**report_body(trip, expense), "message": "   "},
        {**report_body(trip, expense), "message": "hi\x00there"},
    ]
    for body in refused:
        response = await api.post("/v1/support/reports", json=body, headers=bea.headers)
        assert response.status_code == 422, (body, response.text)
    stranger = await sign_in(api, identity_provider, name="Stranger")
    assert (
        await api.post(
            "/v1/support/reports", json=report_body(trip, expense), headers=stranger.headers
        )
    ).status_code == 404
    assert (
        await api.get(
            "/v1/support/checks",
            params={"plan_id": trip.plan_id, "entity_type": "expense"},
            headers=bea.headers,
        )
    ).status_code == 422
    assert (
        await api.get(
            "/v1/support/checks", params={"plan_id": trip.plan_id}, headers=stranger.headers
        )
    ).status_code == 404

    # No plan and no snapshot: just the words.
    plain = await ok(
        await api.post(
            "/v1/support/reports",
            json={
                "category": "sync_issue",
                "message": "Stuck syncing",
                "attach_diagnostics": False,
            },
            headers=stranger.headers,
        ),
        201,
    )
    assert plain["diagnostic_code"] is None

    # Deleting the account deletes its reports.
    gone = await api.delete("/v1/me", headers=stranger.headers)
    assert gone.status_code == 204, gone.text
    assert (
        admin.scalar(
            "SELECT count(*) FROM analytics_ops.problem_reports WHERE user_id = %s",
            stranger.user_id,
        )
        == 0
    )

    monkeypatch.setattr(
        rate_limits, "PROBLEM_REPORTS_PER_USER", RateLimit("problem_report:test", 1, 86_400)
    )
    body = {"category": "other", "message": "Hello", "attach_diagnostics": False}
    await ok(await api.post("/v1/support/reports", json=body, headers=bea.headers), 201)
    limited = await api.post("/v1/support/reports", json=body, headers=bea.headers)
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"


async def test_operators_read_reports_by_code_and_every_read_is_audited(
    api: httpx.AsyncClient, trip: FinancePlan, live_settings: Settings, admin: AdminDatabase
) -> None:
    sent = await ok(
        await api.post(
            "/v1/support/reports",
            json={"category": "sync_issue", "plan_id": trip.plan_id, "message": "Stuck"},
            headers=trip.owner.headers,
        ),
        201,
    )
    runtime = Runtime(
        settings=live_settings,
        database=Database.for_worker(live_settings),
        tokens=AccessTokenCodec(live_settings),
        hasher=TokenHasher.from_settings(live_settings),
        identity_verifier=ExternalIdentityVerifier(live_settings),
    )
    try:
        newest = await list_reports(runtime, operator="ops-kim", limit=5)
        found = await list_reports(
            runtime, operator="ops-kim", code=sent["diagnostic_code"].lower()
        )
    finally:
        await runtime.database.close()
    assert [report.message for report in newest] == ["Stuck"]
    assert [str(report.id) for report in found] == [sent["id"]]
    assert found[0].diagnostics is not None and "plan" in found[0].diagnostics
    assert admin.fetch(
        "SELECT metadata ->> 'operator' FROM sync_audit.audit_events "
        "WHERE action = 'support.problem_report_read' AND entity_id = %s",
        sent["id"],
    ) == [("ops-kim",), ("ops-kim",)]


async def test_a_guests_reports_go_with_the_account_it_merged_into(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    trip: FinancePlan,
    admin: AdminDatabase,
) -> None:
    invite = await ok(
        await api.post(trip.path("/invites"), json={}, headers=trip.owner.headers), 201
    )
    joined = await ok(
        await api.post(
            "/v1/invites/redeem",
            json={"token": invite["token"], "display_name": "Gia", "device": {"platform": "web"}},
        )
    )
    guest = signed_in_from(joined["session"])
    await ok(
        await api.post(
            "/v1/support/reports",
            json={"category": "other", "message": "guest words", "attach_diagnostics": False},
            headers=guest.headers,
        ),
        201,
    )
    await sign_in(api, identity_provider, subject="gia-sub", name="Gia")
    claimed = await ok(
        await api.post(
            "/v1/auth/google",
            json={"id_token": identity_provider.id_token(subject="gia-sub")},
            headers=guest.headers,
        )
    )
    account = signed_in_from(claimed)
    gone = await api.delete("/v1/me", headers=account.headers)
    assert gone.status_code == 204, gone.text
    assert (
        admin.scalar(
            "SELECT count(*) FROM analytics_ops.problem_reports WHERE message = 'guest words'"
        )
        == 0
    )
