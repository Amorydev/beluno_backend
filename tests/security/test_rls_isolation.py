"""Defense in depth: PostgreSQL RLS denies cross-tenant access even if the API is bypassed."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import httpx
import psycopg
import pytest

from beluno.config import Settings
from beluno.modules.invite_links import token_digest
from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub
from beluno.token_hashing import TokenHasher

pytestmark = pytest.mark.integration

TENANT_TABLES = (
    "groups.groups",
    "groups.group_memberships",
    "groups.group_invites",
    "plans.plan_series",
    "plans.plans",
    "plans.plan_participants",
    "plans.plan_invites",
    "plans.travel_plan_details",
    "plans.travel_segments",
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
    group = (
        await api.post(
            "/v1/groups",
            json={"name": "Private", "default_currency": "USD", "default_timezone": "UTC"},
            headers=owner.headers,
        )
    ).json()
    await api.post(f"/v1/groups/{group['id']}/invites", json={}, headers=owner.headers)
    plan = (
        await api.post(
            "/v1/plans",
            json={"title": "Secret", "group_id": group["id"], "visibility": "participants"},
            headers=owner.headers,
        )
    ).json()
    invite = (
        await api.post(f"/v1/plans/{plan['id']}/invites", json={}, headers=owner.headers)
    ).json()
    await api.put(f"/v1/plans/{plan['id']}/travel", json={"notes": "x"}, headers=owner.headers)
    await api.post(
        f"/v1/plans/{plan['id']}/travel/segments",
        json={"segment_type": "car", "timing_mode": "date", "start_date": "2026-12-01"},
        headers=owner.headers,
    )
    await api.post(
        "/v1/plan-series",
        json={
            "group_id": group["id"],
            "title": "Weekly",
            "timezone": "UTC",
            "start_date": "2027-01-04",
            "recurrence_rule": "FREQ=WEEKLY",
        },
        headers=owner.headers,
    )
    return {
        "owner": owner.user_id,
        "outsider": outsider.user_id,
        "group": group["id"],
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
            "display_name, role, access_state, rsvp_status, version, created_at, updated_at) "
            "VALUES (%s, %s, 'user', %s, 'Intruder', 'admin', 'active', 'invited', 1, "
            "now(), now())",
            (str(uuid4()), tenant["plan"], tenant["outsider"]),
        ),
        (
            "INSERT INTO groups.group_memberships (group_id, user_id, role, state, version, "
            "created_at, updated_at) VALUES (%s, %s, 'admin', 'active', 1, now(), now())",
            (tenant["group"], tenant["outsider"]),
        ),
        (
            "INSERT INTO plans.plans (id, group_id, is_series_exception, title, kind, state, "
            "timing_mode, base_currency, visibility, created_by_user_id, version, created_at, "
            "updated_at) VALUES (%s, %s, false, 'x', 'custom', 'planning', 'undecided', 'USD', "
            "'group', %s, 1, now(), now())",
            (str(uuid4()), tenant["group"], tenant["outsider"]),
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
        segments = api_connection.execute("SELECT count(*) FROM plans.travel_segments").fetchone()
    assert plans == [(tenant["plan"],)]
    assert segments == (0,)
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
        assert connection.execute("SELECT count(*) FROM groups.groups").fetchone() == (0,)
        # Series enumeration is the worker's one cross-tenant read.
        assert connection.execute("SELECT count(*) FROM plans.plan_series").fetchone() == (1,)
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
        "WHERE c.relnamespace IN ('iam'::regnamespace, 'groups'::regnamespace, "
        "'plans'::regnamespace, 'sync_audit'::regnamespace) AND c.relkind = 'r'"
    )
    assert len(tables) == 19
    assert all(rls and owner == "migrator" for _, rls, owner in tables), tables
