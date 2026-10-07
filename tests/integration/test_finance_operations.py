"""Ledger operations on real PostgreSQL: reconciliation, repair, limits, atomicity, concurrency."""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from beluno.auth import AccessTokenCodec, AuthenticatedActor
from beluno.config import Settings
from beluno.db.session import Database
from beluno.modules.context import Runtime, open_context
from beluno.modules.finance import maintenance
from beluno.modules.finance.expenses import ExpenseDraft, create_expense
from beluno.modules.finance.splits import Payer, SplitEntry, SplitMethod, SplitSpec
from beluno.modules.iam.external_identity import ExternalIdentityVerifier
from beluno.observability import metrics
from beluno.testkit.database import AdminDatabase
from beluno.testkit.finance import (
    FinancePlan,
    add_expense,
    equal_expense,
    finance_plan,
    if_match,
    ledger_balances,
    pull_all,
)
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher
from beluno.worker import tasks

pytestmark = pytest.mark.integration


@pytest.fixture
async def trip(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> FinancePlan:
    return await finance_plan(api, identity_provider, admin)


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
def reader() -> Iterator[InMemoryMetricReader]:
    meter_reader = InMemoryMetricReader()
    metrics.use_meter_provider(MeterProvider(metric_readers=[meter_reader]))
    yield meter_reader
    metrics.use_meter_provider(None)


def metric_points(reader: InMemoryMetricReader, name: str) -> list[object]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


async def test_reconciler_reports_drift_and_the_audited_rebuild_repairs_it(
    api: httpx.AsyncClient,
    trip: FinancePlan,
    worker: Runtime,
    admin: AdminDatabase,
    reader: InMemoryMetricReader,
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    await add_expense(api, trip.owner, trip, equal_expense(800, ann, [ann, bea]))
    assert await tasks.reconcile_finance_ledgers(0) == 0
    scope = f"plan:{trip.plan_id}"
    _, cursor, _ = await pull_all(api, trip.owner, scope)
    account = admin.scalar("SELECT id FROM finance.ledger_accounts WHERE participant_id = %s", bea)
    admin.execute(
        "UPDATE finance.account_balances SET balance_minor = 7 WHERE account_id = %s", account
    )
    assert await tasks.reconcile_finance_ledgers(0) == 1
    drift = [point.attributes for point in metric_points(reader, "beluno.finance.ledger.drift")]
    assert drift == [{"problem": "balance_drift"}]

    dry = await maintenance.rebuild_balances(
        worker, UUID(trip.plan_id), apply=False, operator="oncall"
    )
    assert [(r.account_id, r.recorded_minor, r.rebuilt_minor) for r in dry] == [(account, 7, -400)]
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events WHERE action = %s",
            "finance.balances_rebuilt",
        )
        == 0
    )
    applied = await maintenance.rebuild_balances(
        worker, UUID(trip.plan_id), apply=True, operator="oncall"
    )
    assert len(applied) == 1
    assert await maintenance.reconcile_plan(worker, UUID(trip.plan_id)) == []
    audit = admin.fetch(
        "SELECT metadata FROM sync_audit.audit_events WHERE action = 'finance.balances_rebuilt'"
    )
    assert audit == [({"plan_id": trip.plan_id, "accounts": 1, "operator": "oncall"},)]
    assert await ledger_balances(api, trip.owner, trip) == {(ann, "USD"): 400, (bea, "USD"): -400}
    # Devices that cached the drifted balance receive the repaired ledger.
    changes, _, _ = await pull_all(api, trip.owner, scope, cursor)
    ledger = (await api.get(trip.path("/ledger"), headers=trip.owner.headers)).json()
    assert [(c["entity_type"], c["entity_id"], c["data"]) for c in changes] == [
        ("ledger", trip.plan_id, ledger)
    ]
    waits = metric_points(reader, "beluno.finance.ledger_lock.wait")
    assert waits and waits[0].count >= 1  # type: ignore[attr-defined]


async def test_finance_writes_are_rate_limited_per_actor_and_plan(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann = trip.people["Ann"]
    refused = equal_expense(100, ann, [ann], currency="XYZ")
    # Limits count in fixed one-minute windows: start early in a window so all
    # 121 requests land in the same one.
    seconds_left = 60 - datetime.now(UTC).second
    if seconds_left < 20:
        await asyncio.sleep(seconds_left + 0.5)
    for _ in range(120):
        response = await api.post(trip.path("/expenses"), json=refused, headers=trip.owner.headers)
        assert response.status_code == 422
    limited = await api.post(trip.path("/expenses"), json=refused, headers=trip.owner.headers)
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    # The same person is not throttled in another plan.
    other = await api.post(
        "/v1/plans",
        json={"type": "hangout", "title": "Other", "base_currency": "USD"},
        headers=trip.owner.headers,
    )
    other_id = other.json()["id"]
    me = other.json()["my_participant"]["id"]
    elsewhere = await api.post(
        f"/v1/plans/{other_id}/expenses",
        json=equal_expense(100, me, [me]),
        headers=trip.owner.headers,
    )
    assert elsewhere.status_code == 201


async def test_a_command_that_fails_before_commit_leaves_nothing_behind(
    runtime: Runtime, trip: FinancePlan, admin: AdminDatabase
) -> None:
    actor = AuthenticatedActor(
        user_id=UUID(trip.owner.user_id),
        session_id=UUID(trip.owner.session_id),
        authenticated_at=runtime.clock(),
        is_guest=False,
    )
    ann, bea = UUID(trip.people["Ann"]), UUID(trip.people["Bea"])
    draft = ExpenseDraft(
        description="Crash test",
        category="other",
        occurred_on=runtime.clock().date(),
        notes=None,
        amount_minor=500,
        currency="USD",
        payers=(Payer(ann, 500),),
        split=SplitSpec(SplitMethod.EQUAL, (SplitEntry(ann), SplitEntry(bea))),
        split_input={"method": "equal", "participant_ids": [str(ann), str(bea)]},
    )
    with pytest.raises(RuntimeError, match="process died"):
        async with open_context(runtime, actor) as ctx:
            await create_expense(ctx, UUID(trip.plan_id), None, draft)
            raise RuntimeError("process died before the response")
    for table in (
        "finance.plan_ledger_heads",
        "finance.expenses",
        "finance.ledger_postings",
        "finance.account_balances",
    ):
        assert admin.scalar(f"SELECT count(*) FROM {table}") == 0, table
    assert (
        admin.scalar(
            "SELECT count(*) FROM sync_audit.audit_events WHERE action LIKE %s", "finance.%"
        )
        == 0
    )


async def test_concurrent_writers_serialize_on_the_ledger_head(
    api: httpx.AsyncClient, trip: FinancePlan, admin: AdminDatabase
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    expense = await add_expense(api, trip.owner, trip, equal_expense(600, ann, [ann, bea]))
    path = trip.path(f"/expenses/{expense['id']}")
    revisions = await asyncio.gather(
        api.put(path, json=equal_expense(700, ann, [ann, bea]), headers=if_match(1, trip.owner)),
        api.put(path, json=equal_expense(900, bea, [ann, bea]), headers=if_match(1, trip.owner)),
    )
    statuses = sorted(response.status_code for response in revisions)
    assert statuses == [200, 412]
    loser = next(r for r in revisions if r.status_code == 412)
    assert loser.json()["current"]["version"] == 2

    created = await asyncio.gather(
        *(
            api.post(
                trip.path("/expenses"),
                json=equal_expense(100 + n, ann, [ann, bea]),
                headers=trip.owner.headers,
            )
            for n in range(12)
        )
    )
    assert all(response.status_code == 201 for response in created)
    sequences = [
        row[0]
        for row in admin.fetch(
            "SELECT ledger_seq FROM finance.ledger_transactions WHERE plan_id = %s "
            "ORDER BY ledger_seq",
            trip.plan_id,
        )
    ]
    assert sequences == list(range(1, len(sequences) + 1))
    assert admin.fetch("SELECT * FROM finance.reconcile_plan(%s)", trip.plan_id) == []

    key = {**trip.owner.headers, "Idempotency-Key": "double-tap"}
    duplicates = await asyncio.gather(
        *(
            api.post(trip.path("/expenses"), json=equal_expense(5, ann, [ann]), headers=key)
            for _ in range(3)
        )
    )
    assert {response.json()["id"] for response in duplicates} == {duplicates[0].json()["id"]}
    assert sum(r.headers.get("Idempotency-Replayed") == "true" for r in duplicates) == 2


async def test_expense_latency_on_one_plan_stays_small(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    """Local PostgreSQL timing smoke test; staging numbers are measured separately."""

    ann, bea, cam = (trip.people[name] for name in ("Ann", "Bea", "Cam"))
    durations = []
    for n in range(60):
        started = time.perf_counter()
        await add_expense(api, trip.owner, trip, equal_expense(1000 + n, ann, [ann, bea, cam]))
        durations.append(time.perf_counter() - started)
    p95 = statistics.quantiles(durations, n=20)[-1]
    assert p95 < 1.0, f"p95 {p95:.3f}s"
    started = time.perf_counter()
    ledger = await api.get(trip.path("/ledger"), headers=trip.owner.headers)
    assert ledger.status_code == 200 and time.perf_counter() - started < 1.0
    assert ledger.json()["ledger_seq"] == 60


async def test_every_finance_entity_flows_through_the_change_feed(
    api: httpx.AsyncClient, trip: FinancePlan
) -> None:
    ann, bea = trip.people["Ann"], trip.people["Bea"]
    bea_user = trip.members["Bea"]
    scope = f"plan:{trip.plan_id}"
    _, cursor, _ = await pull_all(api, bea_user, scope)
    headers = trip.owner.headers
    await api.post(
        trip.path("/fund/contributions"),
        json={
            "participant_id": ann,
            "currency": "USD",
            "amount_minor": 900,
            "occurred_on": "2026-10-06",
        },
        headers=headers,
    )
    fund = await api.put(trip.path("/fund"), json={"note": "Envelope"}, headers=headers)
    commitment = await api.post(
        trip.path("/commitments"),
        json={"description": "Tickets", "currency": "USD", "amount_minor": 400},
        headers=headers,
    )
    expense = await add_expense(api, trip.owner, trip, equal_expense(300, ann, [ann, bea]))
    settlement = await api.post(
        trip.path("/settlements"),
        json={
            "from_participant_id": bea,
            "to_participant_id": ann,
            "currency": "USD",
            "amount_minor": 150,
            "occurred_on": "2026-10-07",
        },
        headers=bea_user.headers,
    )
    budget = await api.post(
        trip.path("/budgets"), json={"scope": "total", "limit_minor": 5_000}, headers=headers
    )
    await api.delete(trip.path(f"/budgets/{budget.json()['id']}"), headers=headers)

    changes, _, status = await pull_all(api, bea_user, scope, cursor)
    assert status == "ok"
    latest = {(c["entity_type"], c["entity_id"]): c for c in changes}
    assert latest[("expense", expense["id"])]["data"] == expense
    assert latest[("settlement", settlement.json()["id"])]["data"] == settlement.json()
    assert latest[("cost_commitment", commitment.json()["id"])]["data"] == commitment.json()
    assert latest[("fund", trip.plan_id)]["data"] == fund.json()
    assert latest[("budget", budget.json()["id"])]["operation"] == "delete"
    movement = next(c for c in changes if c["entity_type"] == "fund_movement")
    assert movement["data"]["kind"] == "contribution"
    ledger = (await api.get(trip.path("/ledger"), headers=bea_user.headers)).json()
    assert latest[("ledger", trip.plan_id)]["data"] == ledger
    listed = await api.get(trip.path("/settlements"), headers=bea_user.headers)
    assert [s["id"] for s in listed.json()["items"]] == [settlement.json()["id"]]
    single = await api.get(
        trip.path(f"/settlements/{settlement.json()['id']}"), headers=bea_user.headers
    )
    assert single.json() == settlement.json() and single.headers["ETag"] == '"1"'
