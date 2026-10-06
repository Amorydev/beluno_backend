"""The trip-first migration upgrades a database that still holds groups, series, and travel."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config

from alembic import command
from beluno.db.bootstrap import PROJECT_ROOT
from beluno.testkit.database import AdminDatabase
from beluno.testkit.environment import IntegrationEnvironment

pytestmark = pytest.mark.integration

SEED_SQL = """
INSERT INTO iam.users (id, kind, status, display_name, version, created_at, updated_at)
VALUES (%(owner)s, 'registered', 'active', 'Owner', 1, now(), now()),
       (%(member)s, 'registered', 'active', 'Member', 1, now(), now());

INSERT INTO groups.groups (id, name, default_currency, default_timezone, state,
                           created_by_user_id, version, created_at, updated_at)
VALUES (%(group)s, 'Crew', 'USD', 'UTC', 'active', %(owner)s, 1, now(), now());
INSERT INTO groups.group_memberships (group_id, user_id, role, state, joined_at, version,
                                      created_at, updated_at)
VALUES (%(group)s, %(owner)s, 'owner', 'active', now(), 1, now(), now()),
       (%(group)s, %(member)s, 'member', 'active', now(), 1, now(), now());

INSERT INTO plans.plan_series (id, group_id, created_by_user_id, title, kind, base_currency,
                               visibility, timezone, start_date, recurrence_rule,
                               participant_user_ids, horizon_days, state, version, created_at,
                               updated_at)
VALUES (%(series)s, %(group)s, %(owner)s, 'Weekly', 'sport', 'USD', 'group', 'UTC',
        '2027-01-04', 'FREQ=WEEKLY', ARRAY[]::uuid[], 56, 'active', 1, now(), now());

INSERT INTO plans.plans (id, group_id, series_id, occurrence_key, is_series_exception, title,
                         kind, state, timing_mode, base_currency, visibility,
                         created_by_user_id, version, created_at, updated_at)
VALUES (%(trip)s, %(group)s, NULL, NULL, false, 'Trip', 'trip', 'planning', 'undecided', 'USD',
        'group', %(owner)s, 1, now(), now()),
       (%(occurrence)s, %(group)s, %(series)s, '2027-01-04', false, 'Weekly', 'sport',
        'planning', 'undecided', 'USD', 'group', %(owner)s, 1, now(), now());
INSERT INTO plans.plan_participants (id, plan_id, identity_kind, user_id, display_name, role,
                                     access_state, rsvp_status, joined_at, version,
                                     created_at, updated_at)
VALUES (gen_random_uuid(), %(trip)s, 'user', %(owner)s, 'Owner', 'owner', 'active',
        'invited', now(), 1, now(), now()),
       (gen_random_uuid(), %(occurrence)s, 'user', %(owner)s, 'Owner', 'owner', 'active',
        'invited', now(), 1, now(), now());

INSERT INTO plans.travel_plan_details (plan_id, destination_summary, version, created_at,
                                       updated_at)
VALUES (%(trip)s, 'Kyoto', 1, now(), now());
INSERT INTO plans.travel_segments (id, plan_id, segment_type, timing_mode, start_date,
                                   sort_order, version, created_at, updated_at)
VALUES (gen_random_uuid(), %(trip)s, 'car', 'date', '2027-03-18', 0, 1, now(), now());

INSERT INTO sync_audit.change_log (changed_at, scope_type, scope_id, entity_type, entity_id,
                                   entity_version, operation, scope_seq)
VALUES (now(), 'group', %(group)s, 'group', %(group)s, 1, 'upsert', 1),
       (now(), 'plan', %(trip)s, 'plan', %(trip)s, 1, 'upsert', 1);
INSERT INTO sync_audit.scope_heads (scope_type, scope_id, last_seq, floor_seq, generation,
                                    updated_at)
VALUES ('group', %(group)s, 1, 0, 1, now()), ('plan', %(trip)s, 1, 0, 1, now());
INSERT INTO sync_audit.audit_events (id, occurred_at, action, entity_type, entity_id, group_id,
                                     plan_id, metadata)
VALUES (gen_random_uuid(), now(), 'plan.created', 'plan', %(trip)s, %(group)s, %(trip)s, '{}');
"""


def test_upgrade_drops_groups_series_and_travel_but_keeps_plans(
    scratch_database: IntegrationEnvironment,
) -> None:
    settings = scratch_database.settings
    assert settings.migration_database_dsn is not None
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.migration_database_dsn)
    command.upgrade(config, "000006_merged_guest_authorship")

    ids = {
        name: str(uuid4()) for name in ("owner", "member", "group", "series", "trip", "occurrence")
    }
    with psycopg.connect(scratch_database.admin_dsn) as connection, connection.transaction():
        for statement in SEED_SQL.split(";\n"):
            if statement.strip():
                connection.execute(statement, ids)

    command.upgrade(config, "head")

    admin = AdminDatabase(scratch_database.admin_dsn)
    assert admin.scalar("SELECT to_regnamespace('groups')") is None
    for table in ("plans.plan_series", "plans.travel_plan_details", "plans.travel_segments"):
        assert admin.scalar("SELECT to_regclass(%s)", table) is None
    assert (
        admin.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'plans' "
            "AND table_name = 'plans' AND column_name IN "
            "('group_id', 'series_id', 'occurrence_key', 'is_series_exception', 'visibility')"
        )
        == 0
    )
    removed = r"groups\.|plan_series|series_id|travel_|visibility|group_id|'group'"
    assert (
        admin.fetch(
            "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname IN ('iam', 'plans', 'people', 'sync_audit', 'finance') "
            "AND p.prosrc ~ %s",
            removed,
        )
        == []
    )
    assert (
        admin.fetch(
            "SELECT schemaname || '.' || policyname FROM pg_policies "
            "WHERE coalesce(qual, '') || coalesce(with_check, '') ~ %s",
            removed,
        )
        == []
    )
    assert sorted(admin.fetch("SELECT id::text FROM plans.plans")) == sorted(
        [(ids["trip"],), (ids["occurrence"],)]
    )
    shapes = dict(
        (row[0], row[1:])
        for row in admin.fetch(
            "SELECT id::text, type, activity, destinations::text, pass_color IS NOT NULL "
            "FROM plans.plans"
        )
    )
    assert shapes[ids["trip"]] == ("trip", None, "[]", True)
    assert shapes[ids["occurrence"]] == ("hangout", "sport", "[]", True)
    assert admin.fetch(
        "SELECT DISTINCT default_share, avatar_color, capabilities FROM plans.plan_participants"
    ) == [(100, "blue", [])]
    assert admin.fetch("SELECT scope_type, scope_seq FROM sync_audit.change_log") == [("plan", 1)]
    assert admin.fetch("SELECT scope_type FROM sync_audit.scope_heads") == [("plan",)]
    assert admin.scalar("SELECT count(*) FROM sync_audit.audit_events") == 1
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "INSERT INTO sync_audit.scope_heads (scope_type, scope_id, last_seq, floor_seq, "
            "generation, updated_at) VALUES ('group', %s, 1, 0, 1, now())",
            ids["group"],
        )
