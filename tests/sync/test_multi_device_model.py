"""Model-based convergence: two devices, faulty transport, one PostgreSQL truth.

Hypothesis drives sequences of local edits, pushes, and pulls on two devices
while responses are dropped or duplicated. Each device keeps the outbox and
view a real client would (apply a page and its cursor together, replay with
the same operation IDs, apply an upsert only when its version is not older).
After the faults stop, every device must converge on the server's state, no
operation may have applied twice, and every scope's sequence must be contiguous.
Examples are derandomized, so a failure reproduces from the printed example.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from beluno.api.main import create_app
from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.db.session import Database
from beluno.testkit.api_client import SignedIn, sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration

ACTIONS = ("edit", "rsvp", "add_budget", "delete_budget", "push", "pull")
FAULTS = ("ok", "lost", "duplicate")
CATEGORIES = ("food", "lodging", "transport", "activities")
Step = tuple[int, str, str, int]

steps = st.lists(
    st.tuples(
        st.integers(0, 1), st.sampled_from(ACTIONS), st.sampled_from(FAULTS), st.integers(0, 10)
    ),
    min_size=1,
    max_size=20,
)


@dataclass
class Device:
    name: str
    user: SignedIn
    outbox: list[dict[str, Any]] = field(default_factory=list)
    view: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    tombstones: dict[tuple[str, str], int] = field(default_factory=dict)
    cursor: str | None = None
    counter: int = 0

    def entity(self, entity_type: str) -> dict[str, Any] | None:
        for (kind, _), data in self.view.items():
            if kind == entity_type:
                return data
        return None

    def apply(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            key = (item["entity_type"], item["entity_id"])
            if item["operation"] == "delete":
                self.view.pop(key, None)
                self.tombstones[key] = max(self.tombstones.get(key, 0), item["version"])
                continue
            if item["version"] <= self.tombstones.get(key, 0):
                continue
            current = self.view.get(key)
            if current is None or item["version"] >= current["version"]:
                self.view[key] = item["data"]


class Scenario:
    def __init__(self, api: httpx.AsyncClient, plan_id: str, devices: list[Device]) -> None:
        self.api = api
        self.plan_id = plan_id
        self.scope = f"plan:{plan_id}"
        self.devices = devices

    # --- local edits -------------------------------------------------------------

    def enqueue(self, device: Device, command: str, payload: dict[str, Any], **extra: Any) -> None:
        device.counter += 1
        device.outbox.append(
            {
                "operation_id": str(new_id()),
                "command": command,
                "target": {"plan_id": self.plan_id, **extra.pop("target", {})},
                "payload": payload,
                **extra,
            }
        )

    def edit(self, device: Device, seed: int) -> None:
        plan = device.entity("plan")
        if plan is None:
            return
        self.enqueue(
            device,
            "plan.update",
            {"title": f"{device.name} edit {device.counter} ({seed})"},
            expected_version=plan["version"],
        )

    def rsvp(self, device: Device, seed: int) -> None:
        self.enqueue(device, "plan.rsvp", {"status": ("going", "maybe", "declined")[seed % 3]})

    def add_budget(self, device: Device, seed: int) -> None:
        # Category budgets are unique per category, so some adds collide and are refused.
        self.enqueue(
            device,
            "budget.create",
            {
                "id": str(new_id()),
                "scope": "category",
                "category": CATEGORIES[seed % len(CATEGORIES)],
                "limit_minor": 1_000 + seed,
            },
        )

    def delete_budget(self, device: Device, seed: int) -> None:
        budgets = sorted(entity_id for (kind, entity_id) in device.view if kind == "budget")
        if not budgets:
            return
        self.enqueue(
            device, "budget.delete", {}, target={"budget_id": budgets[seed % len(budgets)]}
        )

    # --- transport with faults ----------------------------------------------------

    async def push(self, device: Device, fault: str) -> None:
        if not device.outbox:
            return
        batch = {"device_id": device.name, "operations": list(device.outbox)}
        response = await self.api.post("/v1/sync/push", json=batch, headers=device.user.headers)
        assert response.status_code == 200, response.text
        if fault == "lost":
            return
        if fault == "duplicate":
            response = await self.api.post("/v1/sync/push", json=batch, headers=device.user.headers)
            assert response.status_code == 200, response.text
        self.acknowledge(device, response.json()["results"])

    def acknowledge(self, device: Device, results: list[dict[str, Any]]) -> None:
        by_id = {operation["operation_id"]: operation for operation in device.outbox}
        remaining: list[dict[str, Any]] = []
        for result in results:
            operation = by_id[result["operation_id"]]
            outcome = result["outcome"]
            if outcome in ("retry", "skipped"):
                remaining.append(operation)
                continue
            assert outcome in ("applied", "replayed", "conflict", "rejected"), result
            if outcome in ("applied", "replayed"):
                self.apply_own_result(device, operation, result)
        device.outbox = remaining

    def apply_own_result(
        self, device: Device, operation: dict[str, Any], result: dict[str, Any]
    ) -> None:
        body = result["body"]
        command = operation["command"]
        if command == "plan.update":
            device.apply([self.item("plan", body["id"], body)])
        elif command == "plan.rsvp":
            device.apply([self.item("plan_participant", body["id"], body)])
        elif command == "budget.create":
            device.apply([self.item("budget", body["id"], body)])
        elif command == "budget.delete":
            key = ("budget", operation["target"]["budget_id"])
            deleted = device.view.pop(key, None)
            if deleted is not None:
                device.tombstones[key] = max(device.tombstones.get(key, 0), deleted["version"])

    @staticmethod
    def item(entity_type: str, entity_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "operation": "upsert",
            "version": data["version"],
            "data": data,
        }

    async def pull(self, device: Device, fault: str) -> bool:
        body = {"scopes": [{"scope": self.scope, "cursor": device.cursor}]}
        response = await self.api.post("/v1/sync/pull", json=body, headers=device.user.headers)
        assert response.status_code == 200, response.text
        if fault == "lost":
            return True
        if fault == "duplicate":
            again = await self.api.post("/v1/sync/pull", json=body, headers=device.user.headers)
            assert again.json() == response.json()
        page = response.json()["scopes"][0]
        assert page["status"] == "ok", page
        device.apply(page["changes"])
        device.cursor = page["cursor"]
        return bool(page["has_more"])

    async def run(self, script: list[Step]) -> None:
        for index, action, fault, seed in script:
            device = self.devices[index]
            if action == "push":
                await self.push(device, fault)
            elif action == "pull":
                await self.pull(device, fault)
            else:
                getattr(self, action)(device, seed)
        for device in self.devices:
            for _ in range(10):
                if not device.outbox:
                    break
                await self.push(device, "ok")
            assert device.outbox == [], device.outbox
        for device in self.devices:
            while await self.pull(device, "ok"):
                pass


def shared_state(view: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """The part of a view every device must agree on (not the caller-specific bits)."""

    plan = next(data for (kind, _), data in view.items() if kind == "plan")
    return {
        "title": plan["title"],
        "version": plan["version"],
        "budgets": sorted(entity_id for (kind, entity_id) in view if kind == "budget"),
        "participants": sorted(
            (entity_id, data["rsvp_status"], data["version"])
            for (kind, entity_id), data in view.items()
            if kind == "plan_participant"
        ),
    }


async def fresh_view(
    api: httpx.AsyncClient, user: SignedIn, scope: str
) -> dict[tuple[str, str], dict[str, Any]]:
    device = Device("probe", user)
    scenario = Scenario(api, scope.split(":", 1)[1], [device])
    while await scenario.pull(device, "ok"):
        pass
    return device.view


async def run_example(
    live_settings: Settings,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    script: list[Step],
) -> None:
    admin.truncate_all()
    database = Database(live_settings)
    app = create_app(
        settings=live_settings,
        database=database,
        identity_verifier=identity_provider.verifier(live_settings),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as api:
            owner = await sign_in(api, identity_provider, name="Owner")
            partner = await sign_in(api, identity_provider, name="Partner")
            plan = (
                await api.post(
                    "/v1/plans",
                    json={"type": "hangout", "title": "Trip", "base_currency": "USD"},
                    headers=owner.headers,
                )
            ).json()
            invite = await api.post(
                f"/v1/plans/{plan['id']}/invites", json={"role": "admin"}, headers=owner.headers
            )
            joined = await api.post(
                "/v1/invites/redeem",
                json={"token": invite.json()["token"]},
                headers=partner.headers,
            )
            assert joined.status_code == 200, joined.text
            devices = [Device("owner-phone", owner), Device("partner-phone", partner)]
            scenario = Scenario(api, plan["id"], devices)
            for device in devices:
                while await scenario.pull(device, "ok"):
                    pass
            await scenario.run(script)

            # Every device converged on its own canonical view, and all agree on shared state.
            views = [await fresh_view(api, device.user, scenario.scope) for device in devices]
            for device, canonical in zip(devices, views, strict=True):
                assert device.view == canonical, device.name
            assert shared_state(devices[0].view) == shared_state(devices[1].view)
    finally:
        await database.close()

    # No operation applied twice: budgets on the server equal adds minus deletes.
    adds = admin.scalar(
        "SELECT count(*) FROM sync_audit.operations WHERE command = 'budget.create'"
    )
    deletes = admin.scalar(
        "SELECT count(*) FROM sync_audit.operations WHERE command = 'budget.delete'"
    )
    live_budgets = admin.scalar("SELECT count(*) FROM finance.budgets WHERE deleted_at IS NULL")
    assert live_budgets == adds - deletes
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.operations "
            "WHERE response_status NOT BETWEEN 200 AND 299"
        )
        == 0
    )
    # Sequences are contiguous per scope and match the heads.
    gaps = admin.scalar(
        "SELECT count(*) FROM (SELECT scope_type, scope_id, count(*) AS rows, "
        "max(scope_seq) AS top "
        "FROM sync_audit.change_log GROUP BY 1, 2) s "
        "JOIN sync_audit.scope_heads h USING (scope_type, scope_id) "
        "WHERE s.rows <> s.top OR h.last_seq <> s.top"
    )
    assert gaps == 0


@given(script=steps)
@settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_two_devices_converge_under_dropped_and_duplicated_traffic(
    live_settings: Settings,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    script: list[Step],
) -> None:
    asyncio.run(run_example(live_settings, identity_provider, admin, script))


def test_handwritten_lost_ack_and_duplicate_pages_converge(
    live_settings: Settings, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    script: list[Step] = [
        (0, "edit", "ok", 1),
        (0, "add_budget", "ok", 2),
        (0, "push", "lost", 0),
        (1, "pull", "duplicate", 0),
        (1, "edit", "ok", 3),
        (1, "push", "duplicate", 0),
        (0, "push", "ok", 0),
        (0, "pull", "lost", 0),
        (0, "pull", "ok", 0),
        (1, "delete_budget", "ok", 0),
        (1, "push", "lost", 0),
        (1, "push", "ok", 0),
        (0, "rsvp", "ok", 4),
        (0, "push", "duplicate", 0),
    ]
    asyncio.run(run_example(live_settings, identity_provider, admin, script))
    assert UUID(admin.scalar("SELECT id::text FROM plans.plans")) is not None
