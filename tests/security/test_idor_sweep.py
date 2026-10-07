"""Every route that takes an ID refuses IDs from another tenant.

The routes come from the live OpenAPI document, so a new route is swept as soon
as it exists; a write route without a body below fails the sweep until one is
added. Two attacks per route:

* every ID belongs to the victim (an outsider probing the victim's plan);
* the attacker's own plan with the victim's child IDs (the classic IDOR).

Only 403/404 count as refusals: a 412, 409, or 422 would mean the handler got
past authorization, or never reached it. Afterwards the victim's data must be
exactly as it was.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from beluno.testkit.api_client import SignedIn
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import equal_expense, finance_plan
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.tenants import full_tenant

pytestmark = pytest.mark.integration

Body = Callable[[str, str], dict[str, Any]]


def expense(payer: str, other: str) -> dict[str, Any]:
    return equal_expense(300, payer, [payer, other])


def money(payer: str, other: str) -> dict[str, Any]:
    return {
        "from_participant_id": other,
        "to_participant_id": payer,
        "currency": "USD",
        "amount_minor": 10,
        "occurred_on": "2026-10-07",
    }


def kitty(payer: str, _other: str) -> dict[str, Any]:
    return {
        "participant_id": payer,
        "currency": "USD",
        "amount_minor": 10,
        "occurred_on": "2026-10-07",
    }


# (METHOD, path template) -> body built from two participants of the targeted plan.
BODIES: dict[tuple[str, str], Body] = {
    ("PATCH", "/v1/crews/{crew_id}"): lambda _a, _b: {"name": "Taken"},
    ("PATCH", "/v1/plans/{plan_id}"): lambda _a, _b: {"title": "Taken"},
    ("POST", "/v1/plans/{plan_id}/base-currency"): lambda _a, _b: {
        "currency": "GBP",
        "rate": {"rate": "1.1"},
    },
    ("POST", "/v1/plans/{plan_id}/budgets"): lambda _a, _b: {"scope": "total", "limit_minor": 10},
    ("PATCH", "/v1/plans/{plan_id}/budgets/{budget_id}"): lambda _a, _b: {"limit_minor": 20},
    ("POST", "/v1/plans/{plan_id}/commitments"): lambda _a, _b: {
        "category": "food",
        "description": "Taken",
        "currency": "USD",
        "amount_minor": 10,
    },
    ("PUT", "/v1/plans/{plan_id}/commitments/{commitment_id}"): lambda _a, _b: {
        "description": "Taken",
        "currency": "USD",
        "amount_minor": 10,
        "state": "cancelled",
    },
    ("POST", "/v1/plans/{plan_id}/duplicate"): lambda _a, _b: {"title": "Taken"},
    ("POST", "/v1/plans/{plan_id}/expenses"): expense,
    ("PUT", "/v1/plans/{plan_id}/expenses/{expense_id}"): expense,
    ("POST", "/v1/plans/{plan_id}/expenses/{expense_id}/refunds"): lambda a, _b: {
        "amount_minor": 1,
        "recipient": {"participant_id": a},
    },
    ("PUT", "/v1/plans/{plan_id}/fund"): lambda a, _b: {"custodian_participant_id": a},
    ("POST", "/v1/plans/{plan_id}/fund/contributions"): kitty,
    ("POST", "/v1/plans/{plan_id}/fund/withdrawals"): kitty,
    ("POST", "/v1/plans/{plan_id}/fund/counts"): lambda _a, _b: {
        "currency": "USD",
        "counted_minor": 0,
    },
    ("POST", "/v1/plans/{plan_id}/invites"): lambda _a, _b: {},
    ("POST", "/v1/plans/{plan_id}/ledger/adjustments"): lambda a, b: {
        "currency": "USD",
        "memo": "Taken",
        "entries": [
            {"participant_id": a, "amount_minor": 1},
            {"participant_id": b, "amount_minor": -1},
        ],
    },
    ("POST", "/v1/plans/{plan_id}/ledger/confirm"): lambda _a, _b: {"ledger_seq": 0},
    ("POST", "/v1/plans/{plan_id}/ledger/consolidations"): lambda _a, _b: {
        "base_currency": "USD",
        "rates": [{"currency": "JPY", "rate": "0.0067"}],
    },
    ("PATCH", "/v1/plans/{plan_id}/ledger/settings"): lambda _a, _b: {"settle_tolerance_minor": 0},
    ("POST", "/v1/plans/{plan_id}/ownership-transfer"): lambda _a, b: {
        "new_owner_participant_id": b
    },
    ("POST", "/v1/plans/{plan_id}/participants"): lambda _a, _b: {"placeholder_name": "Taken"},
    ("PATCH", "/v1/plans/{plan_id}/participants/{participant_id}"): lambda _a, _b: {
        "role": "viewer"
    },
    ("POST", "/v1/plans/{plan_id}/participants/{participant_id}/claim-invites"): lambda _a, _b: {},
    ("POST", "/v1/plans/{plan_id}/participants/{participant_id}/review"): lambda _a, _b: {
        "approve": True
    },
    ("PUT", "/v1/plans/{plan_id}/rsvp"): lambda _a, _b: {"status": "declined"},
    ("POST", "/v1/plans/{plan_id}/settlements"): money,
    ("POST", "/v1/plans/{plan_id}/state"): lambda _a, _b: {"state": "cancelled"},
    ("POST", "/v1/plans/{plan_id}/places"): lambda _a, _b: {"name": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/places/{place_id}"): lambda _a, _b: {"name": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/places/{place_id}/reaction"): lambda _a, _b: {"wants": False},
    ("POST", "/v1/plans/{plan_id}/places/{place_id}/add-to-plan"): lambda _a, _b: {},
    ("POST", "/v1/plans/{plan_id}/itinerary"): lambda _a, _b: {"title": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/itinerary/{item_id}"): lambda _a, _b: {"title": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/itinerary/{item_id}/attendance"): lambda _a, _b: {
        "status": "not_going"
    },
    ("POST", "/v1/plans/{plan_id}/polls"): lambda _a, _b: {
        "question": "Taken?",
        "options": [{"label": "A"}, {"label": "B"}],
    },
    ("PUT", "/v1/plans/{plan_id}/polls/{poll_id}/vote"): lambda _a, _b: {
        "option_id": "00000000-0000-7000-8000-000000000000"
    },
    ("POST", "/v1/plans/{plan_id}/polls/{poll_id}/outcome"): lambda _a, _b: {
        "action": "add_to_plan"
    },
    ("POST", "/v1/plans/{plan_id}/bookings"): lambda _a, _b: {"kind": "other", "title": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/bookings/{booking_id}"): lambda _a, _b: {
        "kind": "other",
        "title": "Taken",
    },
    ("POST", "/v1/plans/{plan_id}/tasks"): lambda _a, _b: {"title": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/tasks/{task_id}"): lambda _a, _b: {"title": "Taken"},
    ("POST", "/v1/plans/{plan_id}/tasks/{task_id}/status"): lambda _a, _b: {"status": "open"},
    ("POST", "/v1/plans/{plan_id}/packing"): lambda _a, _b: {"name": "Taken"},
    ("POST", "/v1/plans/{plan_id}/packing/templates"): lambda _a, _b: {
        "template_id": "taken",
        "items": [{"name": "Taken"}],
    },
    ("PUT", "/v1/plans/{plan_id}/packing/{packing_item_id}"): lambda _a, _b: {"name": "Taken"},
    ("POST", "/v1/plans/{plan_id}/packing/{packing_item_id}/packed"): lambda _a, _b: {
        "packed": False
    },
    ("PATCH", "/v1/me/passkeys/{passkey_id}"): lambda _a, _b: {"label": "Taken"},
    ("PUT", "/v1/plans/{plan_id}/media/{media_id}/highlight"): lambda _a, _b: {"in_recap": True},
    ("PUT", "/v1/plans/{plan_id}/media/{media_id}/memory"): lambda _a, _b: {"caption": "Taken"},
    ("POST", "/v1/plans/{plan_id}/media"): lambda _a, _b: {
        "kind": "cover",
        "content_type": "image/jpeg",
        "size_bytes": 10,
    },
    ("POST", "/v1/plans/{plan_id}/waivers"): lambda a, b: {
        "debtor_participant_id": b,
        "creditor_participant_id": a,
        "currency": "USD",
        "amount_minor": 10,
        "occurred_on": "2026-10-07",
    },
}


# Values for required query parameters.
QUERY = {"currency": "USD"}


def victim_state(admin: AdminDatabase, plan_id: str, crew_id: str, session_id: str) -> Any:
    tables = admin.fetch(
        "SELECT table_schema || '.' || table_name FROM information_schema.columns "
        "WHERE column_name = 'plan_id' "
        "AND table_schema IN ('plans', 'finance', 'activity', 'schedule_places', 'decisions', "
        "'bookings', 'coordination', 'media_memories') "
        "ORDER BY 1"
    )
    counts = {
        table: admin.scalar(f"SELECT count(*) FROM {table} WHERE plan_id = %s", plan_id)
        for (table,) in tables
    }
    return (
        counts,
        admin.fetch(
            "SELECT version, title, deletion_scheduled_at FROM plans.plans WHERE id = %s", plan_id
        ),
        admin.fetch(
            "SELECT id, version, role, access_state FROM plans.plan_participants "
            "WHERE plan_id = %s ORDER BY id",
            plan_id,
        ),
        admin.fetch(
            "SELECT id, version, state FROM plans.plan_invites WHERE plan_id = %s ORDER BY id",
            plan_id,
        ),
        admin.fetch("SELECT version FROM finance.plan_ledger_heads WHERE plan_id = %s", plan_id),
        admin.fetch("SELECT version, name, deleted_at FROM people.crews WHERE id = %s", crew_id),
        admin.fetch("SELECT revoked_at FROM iam.sessions WHERE id = %s", session_id),
    )


async def test_no_route_accepts_another_tenants_ids(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    victim = await full_tenant(api, identity_provider, admin)
    attacker_plan = await finance_plan(api, identity_provider, admin)
    attacker: SignedIn = attacker_plan.owner
    victim_people = (victim.trip.people["Ann"], victim.trip.people["Dan"])
    attacker_people = (attacker_plan.people["Ann"], attacker_plan.people["Bea"])
    before = victim_state(
        admin, victim.trip.plan_id, victim.ids["crew_id"], victim.ids["session_id"]
    )

    spec = (await api.get("/openapi.json")).json()
    refused: list[str] = []
    accepted: list[str] = []
    for template, operations in sorted(spec["paths"].items()):
        names = re.findall(r"{(\w+)}", template)
        if not names:
            continue
        for method, operation in sorted(operations.items()):
            method = method.upper()
            attacks = [(dict(victim.ids), victim_people, {403, 404})]
            if "plan_id" in names and len(names) > 1:
                mixed = {**victim.ids, "plan_id": attacker_plan.plan_id}
                attacks.append((mixed, attacker_people, {404}))
            for ids, people, allowed in attacks:
                headers = dict(attacker.headers)
                if any(p["name"].lower() == "if-match" for p in operation.get("parameters", [])):
                    headers["If-Match"] = '"1"'
                body = None
                if "requestBody" in operation:
                    build = BODIES.get((method, template))
                    assert build is not None, f"add a body for {method} {template}"
                    body = build(*people)
                path = template.format(**{name: ids[name] for name in names})
                query = {
                    p["name"]: QUERY[p["name"]]
                    for p in operation.get("parameters", [])
                    if p["in"] == "query" and p.get("required")
                }
                response = await api.request(method, path, json=body, params=query, headers=headers)
                attack = "mixed" if ids["plan_id"] != victim.ids["plan_id"] else "outsider"
                label = f"{method} {template} ({attack})"
                if response.status_code in allowed:
                    refused.append(label)
                else:
                    accepted.append(f"{label}: {response.status_code} {response.text[:200]}")

    assert accepted == []
    assert len(refused) >= 60
    assert (
        victim_state(admin, victim.trip.plan_id, victim.ids["crew_id"], victim.ids["session_id"])
        == before
    )
    # Sync gives an outsider nothing of the victim either.
    pulled = await api.post(
        "/v1/sync/pull",
        json={
            "scopes": [
                {"scope": f"plan:{victim.trip.plan_id}", "cursor": None},
                {"scope": f"user:{victim.trip.owner.user_id}", "cursor": None},
            ]
        },
        headers=attacker.headers,
    )
    assert [scope["status"] for scope in pulled.json()["scopes"]] == ["unavailable", "unavailable"]
