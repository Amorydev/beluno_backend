"""The money-alignment migration upgrades a database that already holds finance rows."""

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
VALUES (%(owner)s, 'registered', 'active', 'Owner', 1, now(), now());

INSERT INTO plans.plans (id, type, title, state, timing_mode, base_currency, destinations,
                         pass_color, created_by_user_id, version, created_at, updated_at)
VALUES (%(plan)s, 'trip', 'Trip', 'planning', 'undecided', 'USD', '[]', 'sea', %(owner)s, 1,
        now(), now());
INSERT INTO plans.plan_participants (id, plan_id, identity_kind, user_id, display_name, role,
                                     access_state, rsvp_status, joined_at, default_share,
                                     avatar_color, capabilities, version, created_at, updated_at)
VALUES (%(participant)s, %(plan)s, 'user', %(owner)s, 'Owner', 'owner', 'active', 'invited',
        now(), 100, 'blue', '{}', 1, now(), now());

INSERT INTO finance.plan_ledger_heads (plan_id, ledger_seq, status, disputed_settlements,
                                       version, created_at, updated_at)
VALUES (%(plan)s, 0, 'open', 0, 1, now(), now());
INSERT INTO finance.cost_commitments (id, plan_id, source_type, source_id, commitment_kind,
                                      state, category, description, currency, amount_minor,
                                      base_amount_minor, created_by_user_id, version, created_at,
                                      updated_at)
VALUES (%(commitment)s, %(plan)s, 'manual', %(commitment)s, 'manual', 'committed', 'lodging',
        'Cabin', 'USD', 5000, 5000, %(owner)s, 1, now(), now());
INSERT INTO finance.fund_settings (plan_id, custodian_participant_id, version, created_at,
                                   updated_at)
VALUES (%(plan)s, %(participant)s, 1, now(), now())
"""


def test_existing_finance_rows_read_with_the_new_defaults(
    scratch_database: IntegrationEnvironment,
) -> None:
    settings = scratch_database.settings
    assert settings.migration_database_dsn is not None
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.migration_database_dsn)
    command.upgrade(config, "000007_trip_first_realignment")
    ids = {name: str(uuid4()) for name in ("owner", "plan", "participant", "commitment")}
    with psycopg.connect(scratch_database.admin_dsn) as connection, connection.transaction():
        for statement in SEED_SQL.split(";\n"):
            if statement.strip():
                connection.execute(statement, ids)

    command.upgrade(config, "head")

    admin = AdminDatabase(scratch_database.admin_dsn)
    assert admin.fetch(
        "SELECT count_personal_spend, settle_tolerance_minor, base_change_count "
        "FROM finance.plan_ledger_heads"
    ) == [(True, 0, 0)]
    assert admin.fetch("SELECT base_change_number FROM finance.cost_commitments") == [(0,)]
    assert admin.fetch("SELECT target_currency, target_minor FROM finance.fund_settings") == [
        (None, None)
    ]
    # Every new column with a backfill default leaves no default behind.
    assert (
        admin.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'finance' "
            "AND column_name IN ('source', 'count_personal_spend', 'settle_tolerance_minor', "
            "'base_change_count', 'base_change_number') AND column_default IS NOT NULL"
        )
        == 0
    )
