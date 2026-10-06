"""The ledger schema enforces its invariants even when the application is bypassed."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.db.ids import new_id
from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


def raw_dsn(dsn: str | None) -> str:
    assert dsn is not None
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass
class Tenant:
    owner_user: str
    owner_headers: dict[str, str]
    outsider_user: str
    plan_id: str
    owner: str
    friend: str
    other_plan_id: str
    other_participant: str


@pytest.fixture
async def tenant(api: httpx.AsyncClient, identity_provider: IdentityProviderStub) -> Tenant:
    owner = await sign_in(api, identity_provider, name="Owner")
    outsider = await sign_in(api, identity_provider, name="Outsider")

    async def plan_with_friend(user: Any) -> tuple[str, dict[str, str]]:
        response = await api.post(
            "/v1/plans",
            json={
                "type": "hangout",
                "title": "Trip",
                "base_currency": "USD",
                "participants": [{"placeholder_name": "Friend"}],
            },
            headers=user.headers,
        )
        assert response.status_code == 201, response.text
        plan_id = response.json()["id"]
        people = (await api.get(f"/v1/plans/{plan_id}/participants", headers=user.headers)).json()
        return plan_id, {p["display_name"]: p["id"] for p in people}

    plan_id, people = await plan_with_friend(owner)
    other_plan_id, other_people = await plan_with_friend(outsider)
    return Tenant(
        owner_user=owner.user_id,
        owner_headers=owner.headers,
        outsider_user=outsider.user_id,
        plan_id=plan_id,
        owner=people["Owner"],
        friend=people["Friend"],
        other_plan_id=other_plan_id,
        other_participant=other_people["Friend"],
    )


@pytest.fixture
def api_connection(live_settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(raw_dsn(live_settings.api_database_dsn)) as connection:
        yield connection


def act_as(connection: psycopg.Connection, user_id: str) -> None:
    connection.execute("SELECT set_config('app.actor_id', %s, true)", (user_id,))


@dataclass
class Entry:
    """One hand-written expense: head, accounts, balances, revision, transaction."""

    plan_id: str
    actor: str
    payer: str
    consumers: dict[str, int]
    amount: int = 1000
    head_seq: int = 1
    postings: dict[str, int] | None = None
    payer_amount: int | None = None
    with_transaction: bool = True
    account_plan: str | None = None
    posting_currency: str = "USD"

    def write(self, connection: psycopg.Connection) -> dict[str, str]:
        expense, revision, tx = str(new_id()), str(new_id()), str(new_id())
        people = {self.payer, *self.consumers}
        accounts = {person: str(new_id()) for person in people}
        connection.execute(
            "INSERT INTO finance.plan_ledger_heads (plan_id, ledger_seq, status, "
            "disputed_settlements, count_personal_spend, settle_tolerance_minor, version, "
            "created_at, updated_at) VALUES (%s, %s, 'open', 0, true, 0, 1, now(), now())",
            (self.plan_id, self.head_seq),
        )
        for person, account in accounts.items():
            connection.execute(
                "INSERT INTO finance.ledger_accounts (id, plan_id, kind, participant_id, "
                "currency, created_at) VALUES (%s, %s, 'participant', %s, 'USD', now())",
                (account, self.account_plan or self.plan_id, person),
            )
        connection.execute(
            "INSERT INTO finance.expenses (id, plan_id, state, current_revision_id, "
            "created_by_user_id, version, created_at, updated_at) "
            "VALUES (%s, %s, 'active', %s, %s, 1, now(), now())",
            (expense, self.plan_id, revision, self.actor),
        )
        connection.execute(
            "INSERT INTO finance.expense_revisions (id, plan_id, expense_id, revision_number, "
            "amount_minor, currency, description, category, occurred_on, split_method, "
            "split_algorithm, split_input, base_currency, base_amount_minor, source, "
            "created_by_user_id, created_at) VALUES (%s, %s, %s, 1, %s, 'USD', 'Dinner', "
            "'food', '2026-10-06', 'exact', 'lr-v1', '{}', 'USD', %s, 'http', %s, now())",
            (revision, self.plan_id, expense, self.amount, self.amount, self.actor),
        )
        connection.execute(
            "INSERT INTO finance.expense_payers (revision_id, plan_id, position, participant_id, "
            "amount_minor) VALUES (%s, %s, 0, %s, %s)",
            (revision, self.plan_id, self.payer, self.payer_amount or self.amount),
        )
        for position, (person, owed) in enumerate(self.consumers.items()):
            connection.execute(
                "INSERT INTO finance.expense_splits (revision_id, plan_id, position, "
                "participant_id, owed_minor) VALUES (%s, %s, %s, %s, %s)",
                (revision, self.plan_id, position, person, owed),
            )
        if not self.with_transaction:
            return accounts
        connection.execute(
            "INSERT INTO finance.ledger_transactions (id, plan_id, ledger_seq, kind, expense_id, "
            "revision_id, created_by_user_id, created_at) "
            "VALUES (%s, %s, 1, 'expense', %s, %s, %s, now())",
            (tx, self.plan_id, expense, revision, self.actor),
        )
        postings = self.postings
        if postings is None:
            postings = {self.payer: self.payer_amount or self.amount}
            for person, owed in self.consumers.items():
                postings[person] = postings.get(person, 0) - owed
        for person, amount in postings.items():
            if amount == 0:
                continue
            connection.execute(
                "INSERT INTO finance.ledger_postings (transaction_id, account_id, plan_id, "
                "currency, amount_minor) VALUES (%s, %s, %s, %s, %s)",
                (tx, accounts[person], self.plan_id, self.posting_currency, amount),
            )
            connection.execute(
                "INSERT INTO finance.account_balances (account_id, plan_id, currency, "
                "balance_minor, last_ledger_seq, updated_at) VALUES (%s, %s, 'USD', %s, 1, now())",
                (accounts[person], self.plan_id, amount),
            )
        return accounts


def commit_entry(connection: psycopg.Connection, entry: Entry) -> dict[str, str]:
    with connection.transaction():
        act_as(connection, entry.actor)
        return entry.write(connection)


def test_currency_metadata_is_seeded_with_pinned_exponents(admin: AdminDatabase) -> None:
    exponents = dict(
        admin.fetch(
            "SELECT code, exponent FROM finance.currencies "
            "WHERE code IN ('JPY', 'USD', 'KWD', 'VND', 'EUR')"
        )
    )
    assert exponents == {"JPY": 0, "USD": 2, "KWD": 3, "VND": 0, "EUR": 2}
    assert admin.scalar("SELECT count(*) FROM finance.currencies") == 157


def test_finance_tables_are_rls_protected_and_never_deletable(admin: AdminDatabase) -> None:
    tables = admin.fetch(
        "SELECT c.relname, c.relrowsecurity, pg_get_userbyid(c.relowner) FROM pg_class c "
        "WHERE c.relnamespace = 'finance'::regnamespace AND c.relkind = 'r'"
    )
    assert len(tables) == 24
    assert all(rls and owner == "migrator" for _, rls, owner in tables), tables
    for role in ("api_runtime", "worker_runtime"):
        deletable = admin.fetch(
            "SELECT table_name FROM information_schema.role_table_grants "
            "WHERE table_schema = 'finance' AND grantee = %s "
            "AND privilege_type IN ('DELETE', 'TRUNCATE')",
            role,
        )
        assert deletable == []
    updatable = {
        row[0]
        for row in admin.fetch(
            "SELECT table_name FROM information_schema.role_table_grants "
            "WHERE table_schema = 'finance' AND grantee = 'api_runtime' "
            "AND privilege_type = 'UPDATE'"
        )
    }
    assert updatable == {
        "plan_ledger_heads",
        "account_balances",
        "cost_commitments",
        "expenses",
        "settlements",
        "fund_settings",
        "budgets",
        "consolidations",
    }


def test_a_balanced_expense_commits(
    api_connection: psycopg.Connection, tenant: Tenant, admin: AdminDatabase
) -> None:
    commit_entry(
        api_connection,
        Entry(
            tenant.plan_id,
            tenant.owner_user,
            payer=tenant.owner,
            consumers={tenant.owner: 400, tenant.friend: 600},
        ),
    )
    total = admin.scalar(
        "SELECT sum(amount_minor) FROM finance.ledger_postings WHERE plan_id = %s",
        tenant.plan_id,
    )
    assert total == 0


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"postings": "unbalanced"}, "postings do not sum to zero"),
        ({"postings": "wrong_shape"}, "postings do not match their source entry"),
        ({"payer_amount": 900}, "payers do not add up"),
        ({"split": 500}, "splits do not add up"),
        ({"with_transaction": False}, "revision has no expense transaction"),
        ({"head_seq": 0}, "sequence is ahead of its ledger head"),
    ],
)
def test_deferred_invariants_abort_the_whole_transaction(
    api_connection: psycopg.Connection,
    tenant: Tenant,
    admin: AdminDatabase,
    change: dict[str, Any],
    reason: str,
) -> None:
    entry = Entry(
        tenant.plan_id,
        tenant.owner_user,
        payer=tenant.owner,
        consumers={tenant.owner: 400, tenant.friend: 600},
    )
    if change.get("postings") == "unbalanced":
        entry.postings = {tenant.owner: 600, tenant.friend: -500}
    elif change.get("postings") == "wrong_shape":
        entry.postings = {tenant.owner: 700, tenant.friend: -700}
    elif "split" in change:
        entry.consumers = {tenant.owner: 400, tenant.friend: change["split"]}
        entry.postings = {tenant.owner: 600, tenant.friend: -600}
    else:
        for key, value in change.items():
            setattr(entry, key, value)
    with pytest.raises(psycopg.errors.CheckViolation, match=reason):
        commit_entry(api_connection, entry)
    assert admin.scalar("SELECT count(*) FROM finance.plan_ledger_heads") == 0
    assert admin.scalar("SELECT count(*) FROM finance.ledger_postings") == 0


def test_postings_cannot_cross_plans_or_currencies(
    api_connection: psycopg.Connection, tenant: Tenant
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        commit_entry(
            api_connection,
            Entry(
                tenant.plan_id,
                tenant.owner_user,
                payer=tenant.owner,
                consumers={tenant.friend: 1000},
                posting_currency="EUR",
            ),
        )
    # A participant of another plan cannot hold an account in this one.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        commit_entry(
            api_connection,
            Entry(
                tenant.plan_id,
                tenant.owner_user,
                payer=tenant.owner,
                consumers={tenant.other_participant: 1000},
            ),
        )


def test_history_is_append_only_for_every_role(
    api_connection: psycopg.Connection, tenant: Tenant, admin: AdminDatabase
) -> None:
    commit_entry(
        api_connection,
        Entry(
            tenant.plan_id, tenant.owner_user, payer=tenant.owner, consumers={tenant.friend: 1000}
        ),
    )
    for statement in (
        "UPDATE finance.ledger_postings SET amount_minor = amount_minor * 2",
        "DELETE FROM finance.ledger_transactions",
        "UPDATE finance.expense_revisions SET amount_minor = 1",
        "DELETE FROM finance.expense_splits",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), api_connection.transaction():
            act_as(api_connection, tenant.owner_user)
            api_connection.execute(statement)
        # Even the superuser that bypasses grants and RLS hits the append-only trigger.
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            admin.execute(statement)


def test_outsiders_neither_see_nor_write_another_plans_ledger(
    api_connection: psycopg.Connection, tenant: Tenant
) -> None:
    commit_entry(
        api_connection,
        Entry(
            tenant.plan_id, tenant.owner_user, payer=tenant.owner, consumers={tenant.friend: 1000}
        ),
    )
    with api_connection.transaction():
        act_as(api_connection, tenant.outsider_user)
        for table in ("plan_ledger_heads", "ledger_postings", "expenses", "account_balances"):
            count = api_connection.execute(f"SELECT count(*) FROM finance.{table}").fetchone()
            assert count == (0,), table
    with api_connection.transaction():
        act_as(api_connection, tenant.outsider_user)
        hidden = api_connection.execute(
            "UPDATE finance.plan_ledger_heads SET ledger_seq = 99 WHERE plan_id = %s",
            (tenant.plan_id,),
        )
        assert hidden.rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), api_connection.transaction():
        act_as(api_connection, tenant.outsider_user)
        api_connection.execute(
            "INSERT INTO finance.fund_settings (plan_id, version, created_at, updated_at) "
            "VALUES (%s, 1, now(), now())",
            (tenant.plan_id,),
        )


def test_reconciliation_reports_drift_and_rebuild_repairs_it(
    live_settings: Settings,
    api_connection: psycopg.Connection,
    tenant: Tenant,
    admin: AdminDatabase,
) -> None:
    accounts = commit_entry(
        api_connection,
        Entry(
            tenant.plan_id, tenant.owner_user, payer=tenant.owner, consumers={tenant.friend: 1000}
        ),
    )
    with psycopg.connect(raw_dsn(live_settings.worker_database_dsn)) as worker:
        assert (
            worker.execute("SELECT * FROM finance.reconcile_plan(%s)", (tenant.plan_id,)).fetchall()
            == []
        )
        admin.execute(
            "UPDATE finance.account_balances SET balance_minor = balance_minor + 5 "
            "WHERE account_id = %s",
            accounts[tenant.friend],
        )
        drift = worker.execute(
            "SELECT problem, account_id, expected_minor, actual_minor "
            "FROM finance.reconcile_plan(%s)",
            (tenant.plan_id,),
        ).fetchall()
        assert drift == [("balance_drift", UUID(accounts[tenant.friend]), -1000, -995)]
        preview = worker.execute(
            "SELECT * FROM finance.rebuild_balances(%s, false)", (tenant.plan_id,)
        ).fetchall()
        worker.commit()
        # A dry run leaves the ledger version alone.
        assert preview == [(UUID(accounts[tenant.friend]), -995, -1000, 1)]
        assert worker.execute(
            "SELECT * FROM finance.reconcile_plan(%s)", (tenant.plan_id,)
        ).fetchall()
        applied = worker.execute(
            "SELECT * FROM finance.rebuild_balances(%s, true)", (tenant.plan_id,)
        ).fetchall()
        worker.commit()
        assert applied == [(UUID(accounts[tenant.friend]), -995, -1000, 2)]
        assert (
            worker.execute("SELECT * FROM finance.reconcile_plan(%s)", (tenant.plan_id,)).fetchall()
            == []
        )
        # The worker reaches finance rows only through these gates.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("SELECT count(*) FROM finance.ledger_postings")


def test_the_merge_gate_only_moves_merged_rows_for_participants(
    api_connection: psycopg.Connection, tenant: Tenant
) -> None:
    call = "SELECT finance.transfer_merged_balances(%s, %s, gen_random_uuid(), NULL)"
    lock = "SELECT finance.lock_plan_for_merge(%s)"
    for statement, params in ((call, (tenant.plan_id, tenant.friend)), (lock, (tenant.plan_id,))):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), api_connection.transaction():
            act_as(api_connection, tenant.outsider_user)
            api_connection.execute(statement, params)
    with pytest.raises(psycopg.errors.InvalidParameterValue), api_connection.transaction():
        act_as(api_connection, tenant.owner_user)
        api_connection.execute(call, (tenant.plan_id, tenant.friend))


async def test_mutable_rows_change_only_the_way_their_commands_do(
    api: httpx.AsyncClient,
    api_connection: psycopg.Connection,
    tenant: Tenant,
    admin: AdminDatabase,
) -> None:
    plan = f"/v1/plans/{tenant.plan_id}"
    recorded = await api.post(
        f"{plan}/settlements",
        json={
            "from_participant_id": tenant.friend,
            "to_participant_id": tenant.owner,
            "currency": "USD",
            "amount_minor": 300,
            "occurred_on": "2026-10-06",
        },
        headers=tenant.owner_headers,
    )
    assert recorded.status_code == 201, recorded.text
    settlement = recorded.json()["id"]
    for statement in (
        "UPDATE finance.settlements SET amount_minor = 1, version = version + 1",
        "UPDATE finance.settlements SET status = 'confirmed'",
        "UPDATE finance.plan_ledger_heads SET ledger_seq = ledger_seq - 1",
        "UPDATE finance.account_balances SET currency = 'EUR'",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege, match="does not allow"),
            api_connection.transaction(),
        ):
            act_as(api_connection, tenant.owner_user)
            api_connection.execute(statement)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="does not allow"):
            admin.execute(statement)
    reversed_ = await api.post(
        f"{plan}/settlements/{settlement}/reverse",
        headers={**tenant.owner_headers, "If-Match": '"1"'},
    )
    assert reversed_.status_code == 200, reversed_.text
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="does not allow"),
        api_connection.transaction(),
    ):
        act_as(api_connection, tenant.owner_user)
        api_connection.execute(
            "UPDATE finance.settlements SET status = 'confirmed', version = version + 1"
        )
    # A settlement is posted once, whatever an insider appends by hand.
    with pytest.raises(psycopg.errors.UniqueViolation), api_connection.transaction():
        act_as(api_connection, tenant.owner_user)
        api_connection.execute(
            "UPDATE finance.plan_ledger_heads SET ledger_seq = ledger_seq + 1, "
            "version = version + 1"
        )
        api_connection.execute(
            "INSERT INTO finance.ledger_transactions (id, plan_id, ledger_seq, kind, "
            "settlement_id, created_by_user_id, created_at) "
            "SELECT %s, plan_id, ledger_seq, 'settlement', %s, %s, now() "
            "FROM finance.plan_ledger_heads WHERE plan_id = %s",
            (str(new_id()), settlement, tenant.owner_user, tenant.plan_id),
        )


async def test_a_reversal_stays_with_the_entry_it_reverses(
    api: httpx.AsyncClient,
    api_connection: psycopg.Connection,
    tenant: Tenant,
    admin: AdminDatabase,
) -> None:
    created = []
    for amount in (400, 600):
        response = await api.post(
            f"/v1/plans/{tenant.plan_id}/expenses",
            json={
                "description": "Dinner",
                "occurred_on": "2026-10-06",
                "amount_minor": amount,
                "currency": "USD",
                "payers": [{"participant_id": tenant.owner, "amount_minor": amount}],
                "split": {"method": "equal", "participant_ids": [tenant.friend]},
            },
            headers=tenant.owner_headers,
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
    first, second = created
    source = admin.scalar("SELECT id FROM finance.ledger_transactions WHERE expense_id = %s", first)
    postings = admin.fetch(
        "SELECT account_id, currency, amount_minor FROM finance.ledger_postings "
        "WHERE transaction_id = %s",
        source,
    )
    reversal = str(new_id())
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="filed under another entry"),
        api_connection.transaction(),
    ):
        act_as(api_connection, tenant.owner_user)
        api_connection.execute(
            "UPDATE finance.plan_ledger_heads SET ledger_seq = ledger_seq + 1, "
            "version = version + 1"
        )
        api_connection.execute(
            "INSERT INTO finance.ledger_transactions (id, plan_id, ledger_seq, kind, "
            "expense_id, reverses_transaction_id, created_by_user_id, created_at) "
            "SELECT %s, plan_id, ledger_seq, 'expense_reversal', %s, %s, %s, now() "
            "FROM finance.plan_ledger_heads WHERE plan_id = %s",
            (reversal, second, source, tenant.owner_user, tenant.plan_id),
        )
        for account, currency, amount in postings:
            api_connection.execute(
                "INSERT INTO finance.ledger_postings (transaction_id, account_id, plan_id, "
                "currency, amount_minor) VALUES (%s, %s, %s, %s, %s)",
                (reversal, account, tenant.plan_id, currency, -amount),
            )
