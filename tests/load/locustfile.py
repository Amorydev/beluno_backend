"""Locust scenarios for the release SLOs; request names map one to one to the SLO lines.

    uv run locust -f tests/load/locustfile.py --headless --host http://127.0.0.1:8000 \
        --manifest "$TMPDIR/beluno-load-manifest.json" -u 40 -r 10 -t 3m QuickExpenseUser

Run a scenario alone by naming its class, or omit the class for the mixed run.

| Request name              | SLO (p95)                                  |
|---------------------------|--------------------------------------------|
| quick expense             | 300 ms                                     |
| pull trip bootstrap       | 1 s (every page of a 10-day, 6-person trip)|
| pull trip incremental     | reported, no separate SLO                  |
| settle up preview         | 300 ms                                     |
| settle up record          | 300 ms                                     |
| busy plan expense         | 1 s with 20 concurrent writers             |
| busy plan pull            | reported, no separate SLO                  |
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import date
from typing import Any

import requests
from load_manifest import Assigner, Manifest, Person, Plan, load_manifest
from locust import HttpUser, between, constant_throughput, events, task
from locust.env import Environment
from locust.runners import MasterRunner

PROTOCOL_VERSION = 1
manifest: Manifest | None = None
assigners: dict[str, Assigner] = {}


@events.init_command_line_parser.add_listener
def add_arguments(parser: Any) -> None:
    parser.add_argument("--manifest", help="manifest written by scripts/load_seed.py")


@events.init.add_listener
def load_seed_data(environment: Environment, **_: object) -> None:
    global manifest
    if isinstance(environment.runner, MasterRunner):
        return
    path = getattr(environment.parsed_options, "manifest", None)
    if not path:
        raise SystemExit("--manifest PATH is required (see scripts/load_seed.py)")
    manifest = load_manifest(path)
    assigners["quick"] = Assigner(manifest.quick)
    assigners["trips"] = Assigner(manifest.trips)
    assigners["settle"] = Assigner(manifest.settle)
    assigners["busy"] = Assigner((manifest.busy,))


def today() -> str:
    return date.today().isoformat()


def idempotent(token: str) -> dict[str, str]:
    """Bearer token plus a fresh Idempotency-Key, as the apps send on every write."""

    return {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def expense_body(
    rng: random.Random, plan: Plan, payer: Person, *, split_size: int | None
) -> dict[str, Any]:
    """A food expense paid by ``payer`` and split equally (all people, or a random few)."""

    amount = rng.randrange(500, 20_000)
    ids = plan.participant_ids
    if split_size is not None:
        others = [pid for pid in ids if pid != payer.participant_id]
        ids = [payer.participant_id, *rng.sample(others, k=min(split_size - 1, len(others)))]
    return {
        "description": "Load expense",
        "category": "food",
        "occurred_on": today(),
        "amount_minor": amount,
        "currency": "USD",
        "payers": [{"participant_id": payer.participant_id, "amount_minor": amount}],
        "split": {"method": "equal", "participant_ids": ids},
    }


def pull_scope_page(
    user: HttpUser, plan: Plan, cursor: str | None, name: str
) -> dict[str, Any] | None:
    """One sync pull request for a plan scope, recorded under ``name``."""

    token = getattr(user, "token")  # noqa: B009 - set by BelunoUser.on_start
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "scopes": [{"scope": f"plan:{plan.plan_id}", "cursor": cursor}],
    }
    with user.client.post(
        "/v1/sync/pull", json=body, headers=bearer(token), name=name, catch_response=True
    ) as response:
        if response.status_code != 200:
            response.failure(f"{response.status_code} {response.text[:120]}")
            return None
        page: dict[str, Any] = response.json()["scopes"][0]
        if page["status"] != "ok":
            response.failure(f"scope status {page['status']}")
            return None
        return page


class BelunoUser(HttpUser):
    """One signed-in person pinned to one plan; subclasses pick their pool."""

    abstract = True
    pool = ""
    plan: Plan
    person: Person
    token: str

    def on_start(self) -> None:
        self.plan, self.person = assigners[self.pool].take()
        assert self.person.token is not None
        self.token = self.person.token
        self.rng = random.Random()

    def write(self, name: str, path: str, body: dict[str, Any], token: str | None = None) -> Any:
        """POST a command; 201 is success, anything else (429 included) is a failure."""

        with self.client.post(
            path, json=body, headers=idempotent(token or self.token), name=name, catch_response=True
        ) as response:
            if response.status_code != 201:
                response.failure(f"{response.status_code} {response.text[:120]}")
                return None
            return response.json()


class QuickExpenseUser(BelunoUser):
    """A member adds a dinner to a small plan: the hot path of the app."""

    weight = 3
    pool = "quick"
    wait_time = constant_throughput(0.5)

    @task
    def quick_expense(self) -> None:
        body = expense_body(self.rng, self.plan, self.person, split_size=None)
        self.write("quick expense", f"/v1/plans/{self.plan.plan_id}/expenses", body)


class TripPullUser(BelunoUser):
    """A device opening a trip: full bootstrap now and then, mostly incremental catch-ups."""

    weight = 2
    pool = "trips"
    wait_time = constant_throughput(0.5)
    cursor: str | None = None

    def on_start(self) -> None:
        super().on_start()
        self.walk_session = requests.Session()

    @task(1)
    def bootstrap(self) -> None:
        """Pull every page of the trip; the whole walk is one sample (the SLO is for the trip)."""

        started = time.perf_counter()
        cursor: str | None = None
        length = 0
        error: Exception | None = None
        try:
            while True:
                # A plain session keeps pages out of the per-request stats: only the whole
                # walk is reported.
                response = self.walk_session.post(
                    f"{self.host}/v1/sync/pull",
                    json={
                        "protocol_version": PROTOCOL_VERSION,
                        "scopes": [{"scope": f"plan:{self.plan.plan_id}", "cursor": cursor}],
                    },
                    headers=bearer(self.token),
                    timeout=30,
                )
                length += len(response.content)
                if response.status_code != 200:
                    raise RuntimeError(f"{response.status_code} {response.text[:120]}")
                page = response.json()["scopes"][0]
                if page["status"] != "ok":
                    raise RuntimeError(f"scope status {page['status']}")
                cursor = page["cursor"]
                if not page["has_more"]:
                    break
        except Exception as caught:  # reported as a failed sample, not raised into Locust
            error = caught
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.environment.events.request.fire(
            request_type="POST",
            name="pull trip bootstrap",
            response_time=elapsed_ms,
            response_length=length,
            exception=error,
            context={},
        )
        if error is None:
            self.cursor = cursor

    @task(3)
    def incremental(self) -> None:
        if self.cursor is None:
            self.bootstrap()
            return
        page = pull_scope_page(self, self.plan, self.cursor, "pull trip incremental")
        if page is not None:
            self.cursor = page["cursor"]


class SettleUpUser(BelunoUser):
    """Open the settle-up screen, then record a payment the preview suggested."""

    weight = 1
    pool = "settle"
    wait_time = constant_throughput(0.5)

    @task
    def settle_up(self) -> None:
        path = f"/v1/plans/{self.plan.plan_id}"
        with self.client.get(
            f"{path}/ledger/settlement-preview",
            headers=bearer(self.token),
            name="settle up preview",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code} {response.text[:120]}")
                return
            previews = response.json()
        transfers = [(entry["currency"], item) for entry in previews for item in entry["transfers"]]
        if transfers:
            currency, transfer = self.rng.choice(transfers)
            payer, receiver = transfer["from_participant_id"], transfer["to_participant_id"]
            # Partial payments keep the preview non-empty for the next iteration.
            amount = max(1, min(transfer["amount_minor"], self.rng.randrange(100, 2_000)))
        else:
            currency, amount = "USD", 100
            payer, receiver = (
                person.participant_id for person in self.rng.sample(self.plan.people, 2)
            )
        body = {
            "from_participant_id": payer,
            "to_participant_id": receiver,
            "currency": currency,
            "amount_minor": amount,
            "occurred_on": today(),
        }
        # The payer records it when they have an account; otherwise the owner does (manager).
        actor = next(
            (p for p in self.plan.people if p.participant_id in (payer, receiver) and p.token), None
        )
        self.write(
            "settle up record",
            f"{path}/settlements",
            body,
            token=(actor or self.plan.people[0]).token,
        )


class BusyPlanWriterUser(BelunoUser):
    """Twenty people adding expenses to the same plan at once (ledger-head contention)."""

    fixed_count = 20
    pool = "busy"
    # About one write per second per writer stays under the per-person write limit.
    wait_time = between(0.8, 1.4)
    writes = 0
    cursor: str | None = None

    @task
    def add_expense(self) -> None:
        body = expense_body(self.rng, self.plan, self.person, split_size=3)
        self.write("busy plan expense", f"/v1/plans/{self.plan.plan_id}/expenses", body)
        self.writes += 1
        if self.writes % 10 == 0:
            self.catch_up()

    def catch_up(self) -> None:
        """Every tenth write, read what the other nineteen wrote (cursor kept per user)."""

        page = pull_scope_page(self, self.plan, self.cursor, "busy plan pull")
        if page is not None:
            self.cursor = page["cursor"]
