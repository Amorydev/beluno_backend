"""Defense in depth: PostgreSQL RLS denies cross-tenant access even if the API is bypassed."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from beluno.config import Settings
from beluno.modules.invite_links import token_digest
from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub
from beluno.testkit.tenants import full_tenant
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration

TENANT_TABLES = (
    "plans.plans",
    "plans.plan_participants",
    "plans.plan_invites",
)


def raw_dsn(dsn: str | None) -> str:
    assert dsn is not None
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def api_connection(live_settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(raw_dsn(live_settings.api_database_dsn)) as connection:
        yield connection


def act_as(connection: psycopg.Connection, user_id: str | None, invite_hash: bytes = b"") -> None:
    connection.execute("SELECT set_config('app.actor_id', %s, true)", (user_id or "",))
    connection.execute("SELECT set_config('app.invite_token_hash', %s, true)", (invite_hash.hex(),))


def visible_counts(connection: psycopg.Connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # type: ignore[index]
        for table in TENANT_TABLES
    }


@pytest.fixture
async def tenant(api: httpx.AsyncClient, identity_provider: IdentityProviderStub) -> dict[str, str]:
    owner = await sign_in(api, identity_provider)
    outsider = await sign_in(api, identity_provider)
    plan = (
        await api.post(
            "/v1/plans",
            json={"type": "hangout", "title": "Secret", "base_currency": "USD"},
            headers=owner.headers,
        )
    ).json()
    invite = (
        await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    ).json()
    return {
        "owner": owner.user_id,
        "outsider": outsider.user_id,
        "plan": plan["id"],
        "invite_token": invite["token"],
    }


def test_outsider_sees_no_tenant_rows(
    api_connection: psycopg.Connection, tenant: dict[str, str]
) -> None:
    with api_connection.transaction():
        act_as(api_connection, tenant["owner"])
        owner_view = visible_counts(api_connection)
    assert all(count > 0 for count in owner_view.values()), owner_view
    for actor in (tenant["outsider"], None):
        with api_connection.transaction():
            act_as(api_connection, actor)
            assert visible_counts(api_connection) == dict.fromkeys(TENANT_TABLES, 0)


def test_outsider_writes_are_rejected_by_rls(
    api_connection: psycopg.Connection, tenant: dict[str, str]
) -> None:
    with api_connection.transaction():
        act_as(api_connection, tenant["outsider"])
        updated = api_connection.execute(
            "UPDATE plans.plans SET title = 'hijacked' WHERE id = %s", (tenant["plan"],)
        )
        assert updated.rowcount == 0
    statements = [
        (
            "INSERT INTO plans.plan_participants (id, plan_id, identity_kind, user_id, "
            "display_name, role, access_state, rsvp_status, default_share, avatar_color, "
            "capabilities, version, created_at, updated_at) VALUES (%s, %s, 'user', %s, "
            "'Intruder', 'admin', 'active', 'invited', 100, 'blue', '{}', 1, now(), now())",
            (str(uuid4()), tenant["plan"], tenant["outsider"]),
        ),
        (
            # A plan created in someone else's name.
            "INSERT INTO plans.plans (id, type, title, state, timing_mode, base_currency, "
            "destinations, pass_color, created_by_user_id, version, created_at, updated_at) "
            "VALUES (%s, 'hangout', 'x', 'planning', 'undecided', 'USD', '[]', 'slate', %s, 1, "
            "now(), now())",
            (str(uuid4()), tenant["owner"]),
        ),
    ]
    for statement, params in statements:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), api_connection.transaction():
            act_as(api_connection, tenant["outsider"])
            api_connection.execute(statement, params)


def test_invite_context_exposes_only_the_invited_plan(
    api_connection: psycopg.Connection, tenant: dict[str, str], live_settings: Settings
) -> None:
    hasher = TokenHasher.from_settings(live_settings)
    assert hasher is not None
    digest = token_digest(hasher, tenant["invite_token"])
    with api_connection.transaction():
        act_as(api_connection, tenant["outsider"], digest)
        plans = api_connection.execute("SELECT id::text FROM plans.plans").fetchall()
    assert plans == [(tenant["plan"],)]
    with api_connection.transaction():
        act_as(api_connection, tenant["outsider"], b"\x00" * 32)
        assert api_connection.execute("SELECT count(*) FROM plans.plans").fetchone() == (0,)


def test_runtime_roles_cannot_read_audit_or_delete_domain_rows(
    api_connection: psycopg.Connection, tenant: dict[str, str]
) -> None:
    for statement in (
        "SELECT count(*) FROM sync_audit.audit_events",
        "SELECT count(*) FROM sync_audit.change_log",
        "DELETE FROM plans.plans",
        "DELETE FROM iam.users",
        "UPDATE sync_audit.audit_events SET action = 'x'",
    ):
        with pytest.raises(psycopg.Error), api_connection.transaction():
            act_as(api_connection, tenant["owner"])
            api_connection.execute(statement)


def test_worker_role_is_not_an_rls_bypass(live_settings: Settings, tenant: dict[str, str]) -> None:
    with psycopg.connect(raw_dsn(live_settings.worker_database_dsn)) as connection:
        assert connection.execute("SELECT count(*) FROM plans.plans").fetchone() == (0,)
        # Account lookup by email is reserved for API sign-in flows.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT iam.resolve_user_by_email('a@example.com')")


def test_role_and_table_ownership_invariants(admin: AdminDatabase) -> None:
    roles = dict(
        admin.fetch(
            "SELECT rolname, rolsuper OR rolbypassrls FROM pg_roles WHERE rolname IN "
            "('api_runtime', 'worker_runtime', 'scheduler_runtime', 'migrator')"
        )
    )
    assert roles == dict.fromkeys(
        ("api_runtime", "worker_runtime", "scheduler_runtime", "migrator"), False
    )
    tables = admin.fetch(
        "SELECT c.relname, c.relrowsecurity, pg_get_userbyid(c.relowner) FROM pg_class c "
        "WHERE c.relnamespace IN ('iam'::regnamespace, 'plans'::regnamespace, "
        "'people'::regnamespace, 'activity'::regnamespace, 'sync_audit'::regnamespace) "
        "AND c.relkind = 'r'"
    )
    assert len(tables) == 17
    assert all(rls and owner == "migrator" for _, rls, owner in tables), tables


def rls_tables(admin: AdminDatabase) -> list[str]:
    """Every table under row-level security, read from the catalog."""

    return [
        table
        for (table,) in admin.fetch(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND c.relrowsecurity ORDER BY 1"
        )
    ]


def seen_by(dsn: str, user_id: str, tables: list[str]) -> dict[str, int | None]:
    """Rows each table shows the user; None when the role may not read it at all."""

    seen: dict[str, int | None] = {}
    with psycopg.connect(dsn) as connection:
        for table in tables:
            try:
                with connection.transaction():
                    act_as(connection, user_id)
                    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
                    seen[table] = row[0] if row else 0
            except psycopg.errors.InsufficientPrivilege:
                seen[table] = None
    return seen


# Sign-in and refresh reach these before an actor exists, so their policies are
# open to the API role by design and the API filters them by user. Every other
# RLS table must hide a tenant from an outsider.
CREDENTIAL_TABLES = {
    "iam.email_challenges",
    "iam.passkeys",
    "iam.rate_limit_counters",
    "iam.refresh_tokens",
    "iam.sessions",
    "iam.user_identities",
    "iam.webauthn_challenges",
}

# RLS tables a plan, its money, its people, and a crew never write to.
NOT_TENANT_DATA = {
    "finance.currencies",
    "finance.market_rates",
    "iam.email_challenges",
    "sync_audit.operations",
}


def tenant_rows(table: str, plan_id: str, owner_id: str) -> tuple[str, str]:
    """A predicate selecting the tenant's rows in ``table``, and a column to rewrite."""

    if table == "plans.plans":
        return "id = %s", "title"
    if table == "people.crews":
        return "owner_user_id = %s", "name"
    return "plan_id = %s", "plan_id"


async def test_an_outsider_sees_and_changes_nothing_of_a_full_tenant(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
    live_settings: Settings,
) -> None:
    outsider = await sign_in(api, identity_provider, name="Outsider")
    dsn = raw_dsn(live_settings.api_database_dsn)
    tables = rls_tables(admin)
    totals = {table: admin.scalar(f"SELECT count(*) FROM {table}") for table in tables}
    before = seen_by(dsn, outsider.user_id, tables)

    victim = await full_tenant(api, identity_provider, admin)
    grown = {
        table for table in tables if admin.scalar(f"SELECT count(*) FROM {table}") > totals[table]
    }
    # A new RLS table is either filled by the tenant (and swept) or named above.
    assert set(tables) - grown == NOT_TENANT_DATA
    after = seen_by(dsn, outsider.user_id, tables)
    assert {table for table in tables if after[table] != before[table]} <= CREDENTIAL_TABLES

    owner_id = victim.trip.owner.user_id
    swept = sorted(
        table
        for table in grown
        if table.split(".")[0] in ("plans", "finance", "people", "activity")
    )
    with psycopg.connect(dsn) as connection:
        for table in swept:
            predicate, column = tenant_rows(table, victim.trip.plan_id, owner_id)
            key = victim.trip.plan_id if predicate.startswith(("plan_id", "id")) else owner_id
            [(row,)] = admin.fetch(
                f"SELECT to_jsonb(t) FROM {table} t WHERE {predicate} LIMIT 1", key
            )
            if "id" in row:
                row["id"] = str(uuid4())
            # A copy of a tenant row, in the outsider's session, is refused outright.
            with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
                act_as(connection, outsider.user_id)
                connection.execute(
                    f"INSERT INTO {table} SELECT * FROM jsonb_populate_record(NULL::{table}, %s)",
                    (Jsonb(row),),
                )
            for statement in (
                f"UPDATE {table} SET {column} = {column} WHERE {predicate}",
                f"DELETE FROM {table} WHERE {predicate}",
            ):
                try:
                    with connection.transaction():
                        act_as(connection, outsider.user_id)
                        changed = connection.execute(statement, (key,)).rowcount
                except psycopg.errors.InsufficientPrivilege:
                    changed = 0
                assert changed == 0, f"{statement}: {changed}"
    assert len(swept) >= 25
