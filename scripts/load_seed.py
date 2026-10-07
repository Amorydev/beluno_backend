"""Seed a non-production environment for the Locust scenarios in tests/load.

Registered users and live sessions are written straight to the database (the
migration role) and signed with the configured key; plans, invites, and expenses
go through the real HTTP API so every ledger invariant holds. Nothing in the app
bypasses authentication. The script refuses to run against production settings.

    BELUNO_ENVIRONMENT=development \\
    BELUNO_MIGRATION_DATABASE_URL=... BELUNO_AUTH_SIGNING_KEYS=... \\
    BELUNO_TOKEN_HASH_KEY=... \\
        uv run python scripts/load_seed.py --base-url http://127.0.0.1:8000 \\
        --manifest "$TMPDIR/beluno-load-manifest.json"

The manifest holds live bearer tokens: keep it out of git and delete it after the run.
Tokens last one hour by default; ``--remint MANIFEST`` signs fresh ones for the same
sessions so a longer run can continue.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg

from beluno.auth import AccessTokenCodec
from beluno.config import Environment, Settings
from beluno.db.ids import new_id

TRIP_DAYS = 10
TRIP_PEOPLE = 6
TRIP_EXPENSES = 150
BASE_CURRENCY = "USD"
# Foreign currencies keep the multi-currency ledger paths in the data set (weights in percent).
CURRENCY_MIX = (
    ("USD", 60, (1_500, 30_000)),
    ("EUR", 25, (1_000, 25_000)),
    ("JPY", 15, (800, 30_000)),
)
CATEGORIES = ("food", "lodging", "transport", "activities", "shopping", "groceries", "fees")
# Redeeming an invite is limited per client address; a seed run comes from one address.
CLEAR_RATE_LIMITS_SQL = "DELETE FROM iam.rate_limit_counters"


@dataclass
class Person:
    name: str
    participant_id: str
    user_id: str | None = None
    session_id: str | None = None
    token: str | None = None


@dataclass
class PlanRecord:
    plan_id: str
    kind: str
    expenses: int
    people: list[Person] = field(default_factory=list)


@dataclass
class SeedUser:
    id: UUID
    session_id: UUID
    name: str
    token: str


# --- Users and sessions (database) ----------------------------------------------


class UserFactory:
    """Creates registered users with a live session and a signed access token."""

    def __init__(self, settings: Settings, ttl_seconds: int) -> None:
        dsn = settings.migration_database_dsn
        if dsn is None:
            raise SystemExit("BELUNO_MIGRATION_DATABASE_URL is required to create users")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self._settings = settings.model_copy(update={"auth_access_token_ttl_seconds": ttl_seconds})
        self._codec = AccessTokenCodec(self._settings)
        if not self._codec.configured:
            raise SystemExit("BELUNO_AUTH_SIGNING_KEYS and BELUNO_TOKEN_HASH_KEY are required")
        self._counter = 0

    def create(self, count: int, prefix: str) -> list[SeedUser]:
        now = datetime.now(UTC)
        session_end = now + timedelta(seconds=self._settings.auth_session_max_ttl_seconds)
        idle_end = now + timedelta(seconds=self._settings.auth_session_idle_ttl_seconds)
        users: list[SeedUser] = []
        user_rows, session_rows = [], []
        for _ in range(count):
            self._counter += 1
            user_id, session_id = new_id(), new_id()
            name = f"{prefix} {self._counter}"
            token = self._codec.issue(
                user_id=user_id,
                session_id=session_id,
                authenticated_at=now,
                is_guest=False,
                now=now,
            ).token
            users.append(SeedUser(user_id, session_id, name, token))
            user_rows.append((user_id, name[:80], now, now))
            session_rows.append((session_id, user_id, now, now, now, idle_end, session_end))
        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO iam.users (id, kind, status, display_name, version, created_at, "
                "updated_at) VALUES (%s, 'registered', 'active', %s, 1, %s, %s)",
                user_rows,
            )
            cursor.executemany(
                "INSERT INTO iam.sessions (id, user_id, auth_method, authenticated_at, platform, "
                "device_label, created_at, last_seen_at, idle_expires_at, absolute_expires_at) "
                "VALUES (%s, %s, 'email', %s, 'other', 'load seed', %s, %s, %s, %s)",
                session_rows,
            )
        return users

    def remint(self, user_id: str, session_id: str) -> str:
        now = datetime.now(UTC)
        return self._codec.issue(
            user_id=UUID(user_id),
            session_id=UUID(session_id),
            authenticated_at=now,
            is_guest=False,
            now=now,
        ).token

    def analyze(self) -> None:
        """Fresh tables have no planner statistics; autovacuum would only catch up mid-run."""

        with psycopg.connect(self._dsn, autocommit=True) as connection:
            connection.execute("ANALYZE")

    def clear_rate_limits(self) -> None:
        with psycopg.connect(self._dsn) as connection:
            connection.execute(CLEAR_RATE_LIMITS_SQL)


# --- HTTP helpers ---------------------------------------------------------------


class Api:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def call(
        self,
        method: str,
        path: str,
        token: str | None,
        body: dict[str, Any] | None = None,
        *,
        expect: int,
    ) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if method == "POST" and body is not None:
            headers["Idempotency-Key"] = str(uuid4())
        response = self._client.request(method, path, json=body, headers=headers)
        if response.status_code != expect:
            raise SystemExit(
                f"{method} {path} returned {response.status_code}, expected {expect}: "
                f"{response.text[:300]}"
            )
        return response.json() if response.content else None

    def close(self) -> None:
        self._client.close()


# --- Expense generation ---------------------------------------------------------


def pick_currency(rng: random.Random) -> tuple[str, int]:
    roll = rng.randrange(100)
    threshold = 0
    for code, weight, (low, high) in CURRENCY_MIX:
        threshold += weight
        if roll < threshold:
            return code, rng.randrange(low, high)
    raise AssertionError("currency weights must total 100")


def even_parts(total: int, count: int) -> list[int]:
    parts = [total // count] * count
    parts[0] += total - sum(parts)
    return parts


def build_split(rng: random.Random, amount: int, people: list[str]) -> dict[str, Any]:
    """Equal (everyone or a subset), exact, percentage, or weighted: the mix real trips have."""

    roll = rng.randrange(100)
    if roll < 50:
        return {"method": "equal", "participant_ids": people}
    chosen = rng.sample(people, k=rng.randint(3, len(people)))
    if roll < 70:
        return {"method": "equal", "participant_ids": chosen}
    if roll < 80:
        parts = even_parts(amount, len(chosen))
        shares = [
            {"participant_id": pid, "amount_minor": part}
            for pid, part in zip(chosen, parts, strict=True)
        ]
        return {"method": "exact", "shares": shares}
    if roll < 90:
        parts = even_parts(10_000, len(chosen))
        shares = [
            {"participant_id": pid, "basis_points": part}
            for pid, part in zip(chosen, parts, strict=True)
        ]
        return {"method": "percentage", "shares": shares}
    shares = [{"participant_id": pid, "weight": rng.randint(1, 3)} for pid in chosen]
    return {"method": "shares", "shares": shares}


def build_expense(
    rng: random.Random, people: list[str], day: date, index: int
) -> tuple[str, dict[str, Any]]:
    """Returns ``(payer participant id, request body)``."""

    currency, amount = pick_currency(rng)
    payer = rng.choice(people)
    payers = [{"participant_id": payer, "amount_minor": amount}]
    if rng.randrange(100) < 15:
        second = rng.choice([pid for pid in people if pid != payer])
        half = amount // 2
        payers = [
            {"participant_id": payer, "amount_minor": amount - half},
            {"participant_id": second, "amount_minor": half},
        ]
    body = {
        "description": f"Expense {index}",
        "category": rng.choice(CATEGORIES),
        "occurred_on": day.isoformat(),
        "amount_minor": amount,
        "currency": currency,
        "payers": payers,
        "split": build_split(rng, amount, people),
    }
    return payer, body


# --- Plan building --------------------------------------------------------------


class Seeder:
    def __init__(self, api: Api, users: UserFactory, rng: random.Random) -> None:
        self._api = api
        self._users = users
        self._rng = rng

    def build_plan(
        self,
        *,
        kind: str,
        title: str,
        registered: int,
        placeholders: int,
        expenses: int,
        days: int = 4,
    ) -> PlanRecord:
        """Owner plus invited members and placeholders, then ``expenses`` through the API."""

        people = self._users.create(registered, title)
        owner = people[0]
        start = date(2026, 9, 20)
        placeholder_names = [f"Guest {number + 1}" for number in range(placeholders)]
        body: dict[str, Any] = {
            "type": "trip",
            "title": title[:80],
            "base_currency": BASE_CURRENCY,
            "timing": {
                "mode": "date",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=days - 1)).isoformat(),
            },
            "destinations": [{"name": "Tokyo", "country_code": "JP"}],
            "expected_size": registered + placeholders,
            "participants": [{"placeholder_name": name} for name in placeholder_names],
        }
        plan = self._api.call("POST", "/v1/plans", owner.token, body, expect=201)
        plan_id = plan["id"]
        if registered > 1:
            self._users.clear_rate_limits()
            invite = self._api.call(
                "POST",
                f"/v1/plans/{plan_id}/invites",
                owner.token,
                {"max_uses": registered - 1},
                expect=201,
            )
            for member in people[1:]:
                self._api.call(
                    "POST",
                    "/v1/invites/redeem",
                    member.token,
                    {"token": invite["token"]},
                    expect=200,
                )
        roster = self._api.call("GET", f"/v1/plans/{plan_id}/participants", owner.token, expect=200)
        by_user = {row["user_id"]: row["id"] for row in roster if row["user_id"]}
        by_name = {
            row["display_name"]: row["id"]
            for row in roster
            if row["identity_kind"] == "placeholder"
        }
        record = PlanRecord(plan_id=plan_id, kind=kind, expenses=0)
        for person in people:
            record.people.append(
                Person(
                    name=person.name,
                    participant_id=by_user[str(person.id)],
                    user_id=str(person.id),
                    session_id=str(person.session_id),
                    token=person.token,
                )
            )
        for name in placeholder_names:
            record.people.append(Person(name=name, participant_id=by_name[name]))
        self._add_expenses(record, start, days, expenses)
        return record

    def _add_expenses(self, record: PlanRecord, start: date, days: int, count: int) -> None:
        ids = [person.participant_id for person in record.people]
        tokens = {person.participant_id: person.token for person in record.people if person.token}
        owner_token = record.people[0].token
        for index in range(count):
            day = start + timedelta(days=index * days // max(count, 1))
            payer, body = build_expense(self._rng, ids, day, index + 1)
            # A registered payer records their own expense; placeholders are recorded by the owner.
            self._api.call(
                "POST",
                f"/v1/plans/{record.plan_id}/expenses",
                tokens.get(payer, owner_token),
                body,
                expect=201,
            )
            record.expenses += 1


# --- Entry points ---------------------------------------------------------------


def refuse_production(settings: Settings) -> None:
    if settings.environment is Environment.PRODUCTION:
        raise SystemExit("refusing to seed: BELUNO_ENVIRONMENT is production")


def seed(arguments: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    rng = random.Random(arguments.random_seed)
    users = UserFactory(settings, arguments.token_ttl_seconds)
    api = Api(arguments.base_url)
    seeder = Seeder(api, users, rng)
    try:
        trips = [
            seeder.build_plan(
                kind="trip",
                title=f"Trip {number + 1}",
                registered=TRIP_PEOPLE,
                placeholders=0,
                expenses=TRIP_EXPENSES,
                days=TRIP_DAYS,
            )
            for number in range(arguments.trips)
        ]
        quick = [
            seeder.build_plan(
                kind="quick", title=f"Quick {number + 1}", registered=3, placeholders=1, expenses=3
            )
            for number in range(arguments.quick_plans)
        ]
        settle = [
            seeder.build_plan(
                kind="settle",
                title=f"Settle {number + 1}",
                registered=4,
                placeholders=0,
                expenses=12,
            )
            for number in range(arguments.settle_plans)
        ]
        busy = seeder.build_plan(
            kind="busy",
            title="Busy plan",
            registered=arguments.busy_writers,
            placeholders=2,
            expenses=10,
        )
    finally:
        api.close()
    users.analyze()
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "environment": settings.environment.value,
        "base_url": arguments.base_url,
        "base_currency": BASE_CURRENCY,
        "token_expires_at": (
            datetime.now(UTC) + timedelta(seconds=arguments.token_ttl_seconds)
        ).isoformat(),
        "trips": [asdict(plan) for plan in trips],
        "quick": [asdict(plan) for plan in quick],
        "settle": [asdict(plan) for plan in settle],
        "busy": asdict(busy),
    }


def remint(path: Path, settings: Settings, ttl_seconds: int) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    users = UserFactory(settings, ttl_seconds)
    plans = [*manifest["trips"], *manifest["quick"], *manifest["settle"], manifest["busy"]]
    for plan in plans:
        for person in plan["people"]:
            if person["token"]:
                person["token"] = users.remint(person["user_id"], person["session_id"])
    manifest["token_expires_at"] = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    default_path = Path(tempfile.gettempdir()) / "beluno-load-manifest.json"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API under test")
    parser.add_argument("--manifest", type=Path, default=default_path, help="output manifest path")
    parser.add_argument(
        "--trips", type=int, default=8, help="10-day, 6-person trips (150 expenses)"
    )
    parser.add_argument(
        "--quick-plans", type=int, default=12, help="small plans for quick expenses"
    )
    parser.add_argument("--settle-plans", type=int, default=8, help="plans for settle-up")
    parser.add_argument("--busy-writers", type=int, default=20, help="writers on the busy plan")
    parser.add_argument("--token-ttl-seconds", type=int, default=3_600, help="at most 3600")
    parser.add_argument("--random-seed", type=int, default=7)
    parser.add_argument("--remint", type=Path, help="re-sign tokens in an existing manifest")
    arguments = parser.parse_args()

    settings = Settings()
    refuse_production(settings)
    if not 60 <= arguments.token_ttl_seconds <= 3_600:
        parser.error("--token-ttl-seconds must be between 60 and 3600")
    if arguments.remint:
        write_manifest(
            arguments.remint, remint(arguments.remint, settings, arguments.token_ttl_seconds)
        )
        print(f"re-signed tokens in {arguments.remint}")
        return 0
    manifest = seed(arguments, settings)
    write_manifest(arguments.manifest, manifest)
    people = sum(
        len(plan["people"]) for key in ("trips", "quick", "settle") for plan in manifest[key]
    )
    people += len(manifest["busy"]["people"])
    print(f"manifest written to {arguments.manifest} ({people} people)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
